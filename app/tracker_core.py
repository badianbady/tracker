# -*- coding: utf-8 -*-
"""
Jáchym Air Tracker – aplikace pro Windows
Čte data z MSFS (SimConnect), měří přistání, zobrazuje OBS overlay
a posílá polohu a dokončené lety na web JAI Trackeru.
"""

import csv
import re
import ctypes
import struct
import subprocess
import webbrowser
import io
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- NASTAVENÍ ----------------
SUPABASE_URL = "https://wfmrynrtzrfaqvlwtabz.supabase.co"        # stejné jako v config.js na webu
SUPABASE_KEY = "sb_publishable_onhmEyLR0N0dPCOjNTyVig_ZR4VU2hh"    # stejné jako v config.js na webu
CALLSIGN = "JAI 101"   # použije se, jen když nemáš volací znak v profilu na webu
PORT = 8765
# -------------------------------------------

try:
    import build_info          # vytvoří ho GitHub při sestavení aplikace
    VERSION = build_info.VERSION
    REPO = build_info.REPO
    CHANNEL = getattr(build_info, "CHANNEL", "stable")
except ImportError:
    VERSION = "0.4.0-dev"
    REPO = ""
    CHANNEL = "stable"
BETA = CHANNEL == "beta"

FROZEN = getattr(sys, "frozen", False)
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "JachymAirTracker")
os.makedirs(DATA_DIR, exist_ok=True)
HISTORY_FILE = os.path.join(DATA_DIR, "historie_pristani.json")
ACCOUNT_FILE = os.path.join(DATA_DIR, "ucet.json")
QUEUE_FILE = os.path.join(DATA_DIR, "neodeslane_lety.json")
AIRPORTS_FILE = os.path.join(DATA_DIR, "letiste_v2.json")
RUNWAYS_FILE = os.path.join(DATA_DIR, "drahy_v1.json")
CREW_FILE = os.path.join(DATA_DIR, "crew.json")
RUNWAYS_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/runways.csv"
CREW_URL = "https://crew.jachymair.com"


CUSTOM_WEB_URL = "https://tracker.jachymair.com/"     # vlastní adresa webu


def github_web_url():
    if not REPO or "/" not in REPO:
        return ""
    owner, name = REPO.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{name}/"


def web_url():
    """Adresa webu, kterou aplikace otevírá."""
    return CUSTOM_WEB_URL or github_web_url()


def web_page():
    """Stránka, kterou aplikace načte: testovací verze webu pro betu, jinak hlavní."""
    return "beta.html" if BETA else ""


def trusted_web_urls():
    return [u for u in (CUSTOM_WEB_URL, github_web_url()) if u]

AIRPORTS_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

lock = threading.Lock()


def log(*parts):
    msg = " ".join(str(p) for p in parts).strip()
    try:
        if sys.stdout:
            sys.stdout.write(msg + "\n")
    except Exception:
        pass
    if msg:
        with lock:
            state["log"].append((datetime.now().strftime("%H:%M"), msg))
            del state["log"][:-40]


state = {
    "connected": False,
    "status": "Startuji…",
    "callsign": CALLSIGN,
    "aircraft": "",
    "alt": 0, "agl": 0, "ias": 0, "gs": 0, "vs": 0, "hdg": 0, "g": 1.0,
    "lat": 0.0, "lon": 0.0,
    "on_ground": True,
    "phase": "Na zemi",
    "dep": None,
    "landing": None,
    "demo": False,
    "online": False,
    "web_status": "Nepřihlášen",
    "username": None,
    "log": [],
    "update": None,
    "plan": None,          # platný plán = SimBrief + ruční úpravy
    "sb_plan": None,       # plán ze SimBriefu
    "flight_info": {},     # ručně zadané údaje o letu
    "squawk": None,
    "live_landing": None,
    "reg_default": False,
    "reg": None,
    "ac_model": None,
    "va_choice": None,
    "reg_override": None,
    "plan_ver": 0,
    "simbrief_user": None,
    "crew": {"linked": False, "ident": None, "name": None, "auto": True, "status": None, "bids": []},
    "last_flight": None,
    "trail": [],
    "trail_ver": 0,
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def haversine_nm(lat1, lon1, lat2, lon2):
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


# ---------------- Letiště ----------------
AIRPORTS = []


def load_airports():
    """Při prvním spuštění stáhne databázi letišť (OurAirports) a uloží ji do letiste.json."""
    global AIRPORTS
    if os.path.exists(AIRPORTS_FILE):
        try:
            with open(AIRPORTS_FILE, "r", encoding="utf-8") as f:
                AIRPORTS = json.load(f)
            return
        except Exception:
            pass
    log("Stahuji databázi letišť (jen poprvé)…")
    try:
        with urllib.request.urlopen(AIRPORTS_URL, timeout=60) as r:
            text = r.read().decode("utf-8")
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            if row["type"] not in ("large_airport", "medium_airport", "small_airport"):
                continue
            code = row.get("icao_code") or row.get("gps_code") or row.get("ident")
            size = {"large_airport": 3, "medium_airport": 2}.get(row["type"], 1)
            if "-" in code:
                size = 0
            try:
                out.append([code, row["name"], round(float(row["latitude_deg"]), 4), round(float(row["longitude_deg"]), 4), size])
            except ValueError:
                continue
        with open(AIRPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        AIRPORTS = out
        log(f"Databáze letišť připravena ({len(out)} letišť).")
    except Exception as e:
        log("Databázi letišť se nepodařilo stáhnout, lety budou bez ICAO kódů:", e)


RUNWAYS = []


def load_runways():
    """Databáze drah (OurAirports) pro rozbor dosednutí."""
    global RUNWAYS
    if os.path.exists(RUNWAYS_FILE):
        try:
            with open(RUNWAYS_FILE, "r", encoding="utf-8") as f:
                RUNWAYS = json.load(f)
            return
        except Exception:
            pass
    try:
        with urllib.request.urlopen(RUNWAYS_URL, timeout=60) as r:
            text = r.read().decode("utf-8")
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("closed") == "1":
                continue
            try:
                le = [row["le_ident"], float(row["le_latitude_deg"]), float(row["le_longitude_deg"]),
                      float(row.get("le_displaced_threshold_ft") or 0)]
                he = [row["he_ident"], float(row["he_latitude_deg"]), float(row["he_longitude_deg"]),
                      float(row.get("he_displaced_threshold_ft") or 0)]
            except (ValueError, KeyError):
                continue
            width = float(row.get("width_ft") or 150)
            out.append([row["airport_ident"], le, he, width])
        with open(RUNWAYS_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f)
        RUNWAYS = out
        log(f"Databáze drah připravena ({len(out)} drah).")
    except Exception as e:
        log("Databázi drah se nepodařilo stáhnout:", e)


def runway_at(lat, lon, hdg):
    """Najde dráhu, na kterou letadlo dosedlo, a spočítá vzdálenost za prahem a odchylku od osy."""
    best = None
    coslat = math.cos(math.radians(lat))
    for apt, a, b, width in RUNWAYS:
        if abs(a[1] - lat) > 0.06 or abs(a[2] - lon) > 0.1:
            continue
        for thr, end in ((a, b), (b, a)):
            ex = (end[2] - thr[2]) * coslat * 111320
            ey = (end[1] - thr[1]) * 110540
            length = math.hypot(ex, ey)
            if length < 100:
                continue
            brg = math.degrees(math.atan2(ex, ey)) % 360
            if abs((hdg - brg + 180) % 360 - 180) > 40:
                continue
            px = (lon - thr[2]) * coslat * 111320
            py = (lat - thr[1]) * 110540
            along = (px * ex + py * ey) / length
            cross = (px * ey - py * ex) / length      # kladné = vpravo od osy
            if not (-150 <= along <= length + 50) or abs(cross) > width * 0.3048 / 2 + 40:
                continue
            if best is None or abs(cross) < abs(best["offset_m"]):
                best = {"rwy": thr[0], "airport": apt,
                        "dist_m": round(along - thr[3] * 0.3048),
                        "offset_m": round(cross, 1), "length_m": round(length)}
    return best


def fetch_metars(ids):
    """Aktuální METAR pro letiště (přes funkci trackeru). Vrací {ICAO: zkrácený záznam}."""
    ids = [i for i in dict.fromkeys(i for i in ids if i and re.match(r"^[A-Z0-9]{4}$", i))]
    if not ids:
        return {}
    try:
        req = urllib.request.Request(f"{SUPABASE_URL.rstrip('/')}/functions/v1/metar?ids={','.join(ids)}", headers={"apikey": SUPABASE_KEY})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}
    out = {}
    for m in data if isinstance(data, list) else []:
        icao = m.get("icaoId")
        if icao in ids and icao not in out:
            out[icao] = {k: m.get(k) for k in ("rawOb", "obsTime", "temp", "dewp", "wdir", "wspd", "wgst", "visib", "altim", "fltCat")}
    return out


def attach_weather(flight, dep_wx):
    """Počasí k letu: odlet z doby vzletu, cíl a záloha z doby přistání (na pozadí, ať se nebrzdí záznam)."""
    def run():
        arr, altn = flight.get("arr_icao"), flight.get("plan_altn")
        got = fetch_metars([arr, altn])
        wx = {}
        if dep_wx:
            wx["dep"] = dep_wx
        if arr in got:
            wx["arr"] = dict(got[arr], icao=arr)
        if altn in got:
            wx["altn"] = dict(got[altn], icao=altn)
        if wx:
            flight["wx"] = wx
    threading.Thread(target=run, daemon=True).start()


def approach_analysis(buf, td_lat, td_lon, hdg):
    """Rozbor přiblížení: profil posledních ~10 nm a kontrola stabilizace v 1000 a 500 ft nad zemí."""
    if not buf or len(buf) < 8:
        return None
    thr = None
    for apt, a, b, width in RUNWAYS:
        if abs(a[1] - td_lat) > 0.06 or abs(a[2] - td_lon) > 0.1:
            continue
        for t0, t1 in ((a, b), (b, a)):
            f = _rwy_frame(td_lat, td_lon, t0, t1)
            if not f:
                continue
            along, cross, length, brg = f
            if -150 <= along <= length + 50 and abs(cross) <= width * 0.3048 / 2 + 40 and abs((hdg - brg + 180) % 360 - 180) < 40:
                thr = (t0, t1)
    prof = []
    for (t, lat, lon, agl, ias, gs, vs, h) in buf:
        if thr:
            f = _rwy_frame(lat, lon, thr[0], thr[1])
            if not f:
                continue
            along, cross, _, _ = f
            dist_nm = -along / 1852
        else:
            dist_nm, cross = haversine_nm(lat, lon, td_lat, td_lon), None
        if 0 <= dist_nm <= 10.5:
            prof.append([round(dist_nm, 2), int(agl), int(ias), int(vs), None if cross is None else round(cross)])
    if len(prof) < 5:
        return None
    prof.sort(key=lambda x: -x[0])
    finals = [x[2] for x in prof if 150 <= x[1] <= 500]
    vapp = sorted(finals)[len(finals) // 2] if finals else None

    def gate(ft):
        near = min(prof, key=lambda x: abs(x[1] - ft))
        if abs(near[1] - ft) > 250:
            return None
        dist, agl, ias, vs, cross = near
        dev = round(agl - dist * 318.0)          # odchylka od 3° sestupové roviny nad prahem
        checks = {
            "vs": abs(vs) <= 1000,
            "speed": vapp is None or abs(ias - vapp) <= (15 if ft >= 1000 else 10),
            "path": abs(dev) <= (200 if ft >= 1000 else 120),
            "lateral": cross is None or abs(cross) <= (200 if ft >= 1000 else 90),
        }
        return {"agl": agl, "dist": dist, "ias": ias, "vs": vs, "dev": dev, "cross": cross, "ok": all(checks.values()), "checks": checks}

    g1000, g500 = gate(1000), gate(500)
    while len(prof) > 160:
        prof = prof[::2]
    return {"profile": prof, "vapp": vapp, "g1000": g1000, "g500": g500,
            "stable": bool(g500 and g500["ok"] and (g1000 is None or g1000["ok"])), "rwy_ok": thr is not None}


def runway_text(info):
    if not info:
        return None
    off = info["offset_m"]
    side = "na ose" if abs(off) < 0.5 else f"{abs(off):.1f} m {'vpravo' if off > 0 else 'vlevo'} od osy".replace(".", ",")
    dist = info["dist_m"]
    where = f"{dist} m za prahem" if dist >= 0 else f"{-dist} m před prahem"
    return f"RWY {info['rwy']} · {where} · {side}"


def squawk_text(v):
    """Kód transpondéru: simulátor ho posílá v BCO16 (každá číslice = jedna hex číslice)."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    h = format(v, "x")
    if len(h) <= 4 and all(c in "01234567" for c in h):
        return h.zfill(4)
    d = str(v)
    if len(d) <= 4 and all(c in "01234567" for c in d):
        return d.zfill(4)
    return None


def wind_components(wind_dir, wind_kt, hdg):
    """Čelní (kladná) / zadní (záporná) a boční (kladná = zprava) složka větru."""
    if wind_dir is None or wind_kt is None:
        return None, None
    rel = math.radians((wind_dir - hdg + 360) % 360)
    return round(wind_kt * math.cos(rel)), round(wind_kt * math.sin(rel))


BONUS_NM = {3: 3.0, 2: 1.5, 1: 0.0, 0: -1.0}


def nearest_airport(lat, lon, max_nm=8):
    """Nejbližší letiště; velká letiště mají přednost před malými plochami poblíž."""
    best, best_d = None, None
    for code, name, alat, alon, size in AIRPORTS:
        if abs(alat - lat) > 0.3:
            continue
        d = haversine_nm(lat, lon, alat, alon)
        if d > max_nm:
            continue
        eff = d - BONUS_NM.get(size, 0)
        if best_d is None or eff < best_d:
            best, best_d = (code, name), eff
    return best


# ---------------- Web (Supabase) ----------------
class Cloud:
    def __init__(self):
        self.enabled = not SUPABASE_URL.startswith("VLOZ")
        self.access = None
        self.refresh_token = None
        self.expires = 0
        self.user_id = None
        self.profile = None
        self.lock = threading.Lock()

    def _http(self, method, path, body=None, token=None, prefer=None):
        url = SUPABASE_URL.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if prefer:
            req.add_header("Prefer", prefer)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"message": raw.decode("utf-8", "ignore")}

    def _store(self, res):
        self.access = res["access_token"]
        self.refresh_token = res["refresh_token"]
        self.expires = time.time() + int(res.get("expires_in", 3600))
        self.user_id = res["user"]["id"]
        with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"refresh_token": self.refresh_token, "email": res["user"].get("email")}, f)

    def has_saved(self):
        return os.path.exists(ACCOUNT_FILE)

    def try_saved(self):
        """Přihlášení uloženým tokenem. Vrací True / False / None (web nedostupný)."""
        try:
            with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            return False
        self.refresh_token = saved.get("refresh_token")
        try:
            if self._refresh():
                self._load_profile()
                return True
            return False
        except urllib.error.URLError:
            return None

    def login(self, email, password):
        """Vrací (True, '') nebo (False, 'důvod')."""
        if not self.enabled:
            return False, "Aplikace nemá nastavený web."
        try:
            code, res = self._http("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password})
        except urllib.error.URLError:
            return False, "Nepodařilo se připojit k internetu."
        if code == 200 and res and res.get("access_token"):
            self._store(res)
            self._load_profile()
            return True, ""
        if code == 400:
            return False, "Špatný e-mail nebo heslo."
        return False, str((res or {}).get("msg") or (res or {}).get("message") or code)

    def logout(self):
        try:
            os.remove(ACCOUNT_FILE)
        except OSError:
            pass
        self.access = self.refresh_token = self.user_id = self.profile = None
        with lock:
            state["online"] = False
            state["username"] = None
            state["web_status"] = "Nepřihlášen"
            state["callsign"] = CALLSIGN

    def _refresh(self):
        if not self.refresh_token:
            return False
        code, res = self._http("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": self.refresh_token})
        if code == 200 and res and res.get("access_token"):
            self._store(res)
            return True
        return False

    def _load_profile(self):
        code, res = self.request("GET", f"/rest/v1/profiles?id=eq.{self.user_id}&select=username,callsign,simbrief_user")
        if code == 400:
            code, res = self.request("GET", f"/rest/v1/profiles?id=eq.{self.user_id}&select=username,callsign")
        if code == 200 and res:
            self.profile = res[0]
            cs = self.profile.get("username") or CALLSIGN
            with lock:
                state["callsign"] = cs
                state["simbrief_user"] = self.profile.get("simbrief_user")
                state["online"] = True
                state["username"] = self.profile.get("username")
                state["web_status"] = "Přihlášen"
            log(f"Přihlášen jako {self.profile.get('username')} ({cs})")
        else:
            with lock:
                state["online"] = True
                state["web_status"] = "Přihlášen"
        return True

    def request(self, method, path, body=None, prefer=None):
        with self.lock:
            if time.time() > self.expires - 120:
                self._refresh()
            token = self.access
        code, res = self._http(method, path, body, token, prefer)
        if code == 401:
            with self.lock:
                self._refresh()
                token = self.access
            code, res = self._http(method, path, body, token, prefer)
        return code, res

    def save_profile(self, fields):
        code, res = self.request("PATCH", f"/rest/v1/profiles?id=eq.{self.user_id}", fields, "return=minimal")
        return code in (200, 204)

    def call_function(self, name, body):
        with self.lock:
            if time.time() > self.expires - 120:
                self._refresh()
            token = self.access
        return self._http("POST", f"/functions/v1/{name}", body, token)

    def send_live(self, s, with_trail=False, with_plan=False):
        body = {
            "user_id": self.user_id, "callsign": s["callsign"], "aircraft": s["aircraft"][:80],
            "lat": s["lat"], "lon": s["lon"], "alt": s["alt"], "gs": s["gs"], "hdg": s["hdg"],
            "vs": s["vs"], "phase": s["phase"], "dep_icao": (s.get("dep") or {}).get("icao"),
        }
        body["reg"] = current_reg(s)
        body["ac_icao"] = ((s.get("plan") or {}).get("aircraft") or s.get("ac_model") or None)
        body["va"] = current_va(s)
        body["squawk"] = s.get("squawk")
        body["landing"] = s.get("live_landing")
        body["takeoff_at"] = (s.get("dep") or {}).get("iso")
        if with_trail:
            body["trail"] = s["trail"]
        if with_plan:
            body["plan"] = s["plan"]
        if s.get("plan") and s["plan"].get("callsign"):
            body["callsign"] = s["plan"]["callsign"]
        return self.request("POST", "/rest/v1/live?on_conflict=user_id", body, "resolution=merge-duplicates,return=minimal")

    def send_flight(self, flight):
        body = clean_flight(flight)
        code, res = self.request("POST", "/rest/v1/flights", body, "return=minimal")
        for col in ("approach", "wx"):                 # databáze ještě nemá nový sloupec – uložit bez něj
            if code not in (200, 201) and col in str((res or {}).get("message", "")):
                body.pop(col, None)
                code, res = self.request("POST", "/rest/v1/flights", body, "return=minimal")
        if code in (200, 201):
            return True, ""
        return False, str((res or {}).get("message") or code)


cloud = Cloud()


# ---------------- Virtuální aerolinka ----------------
SETTINGS_FILE = os.path.join(DATA_DIR, "nastaveni.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        with lock:
            state["va_choice"] = d.get("va")
            state["reg_override"] = d.get("reg")
        TAIL_SEEN.update(d.get("tails") or {})
        atc.auto = False          # automatický kopilot zrušen – odpovídá pilot
        atc.tune = bool(d.get("atc_tune", False))
        VOICE_PREFS.update(d.get("voices") or {})
        atc.simbrief = bool(d.get("atc_simbrief", True))
    except Exception:
        pass


def _save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"va": state.get("va_choice"), "reg": state.get("reg_override"), "tails": TAIL_SEEN,
                       "atc_auto": atc.auto, "atc_simbrief": atc.simbrief, "atc_tune": atc.tune, "voices": VOICE_PREFS}, f)
    except Exception:
        pass


def set_va(code):
    code = (code or "").strip().upper()[:8] or None
    if code == "JAI":
        code = None       # JAI se zapíná samo podle rezervace v crew centru
    with lock:
        state["va_choice"] = code
    _save_settings()


def current_reg(s):
    """Registrace: ručně zadaná > ze SimBriefu > ze simulátoru (jen pokud to není výchozí registrace MSFS)."""
    sim = None if s.get("reg_default") else s.get("reg")
    return s.get("reg_override") or (s.get("plan") or {}).get("reg") or sim or None


TAIL_SEEN = {}     # registrace ze simulátoru -> názvy letadel, u kterých se objevila


def note_sim_reg(reg, title):
    """MSFS hlásí výchozí registraci u každé livery bez vlastní registrace. Když stejnou uvidíme
    u dvou různých letadel, je to výchozí registrace a nepoužijeme ji."""
    if not reg:
        return False
    titles = TAIL_SEEN.setdefault(reg, [])
    if title and title not in titles:
        titles.append(title)
        del titles[:-5]
        _save_settings()
    return len(titles) >= 2


def set_reg(reg):
    reg = "".join(ch for ch in str(reg or "").upper() if ch.isalnum() or ch == "-")[:12] or None
    with lock:
        state["reg_override"] = reg
    _save_settings()


def current_va(s, dep=None):
    """Let je za Jáchym Air jen tehdy, když pilot letí linku, kterou má rezervovanou v crew centru."""
    dep = dep or (s.get("dep") or {}).get("icao") or (s.get("plan") or {}).get("dep")
    arr = (s.get("plan") or {}).get("arr")
    for b in (s.get("crew") or {}).get("bids") or []:
        if dep and b.get("dep") == dep and (not arr or b.get("arr") == arr):
            return "JAI"
    return s.get("va_choice") or None


# ---------------- SimBrief ----------------
def fetch_simbrief(user):
    """Stáhne poslední OFP ze SimBriefu. Vrací (plan, chyba)."""
    user = (user or "").strip()
    if not user:
        return None, "Zadej své uživatelské jméno ze SimBriefu."
    param = "userid" if user.isdigit() else "username"
    url = f"https://www.simbrief.com/api/xml.fetcher.php?{param}={urllib.parse.quote(user)}&json=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JachymAirTracker"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ofp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("fetch", {}).get("status")
        except Exception:
            msg = None
        return None, msg or f"SimBrief odpověděl chybou {e.code}. Zkontroluj jméno."
    except Exception as e:
        return None, f"SimBrief se nepodařilo načíst: {e}"

    def g(d, *keys):
        for k in keys:
            d = d.get(k) if isinstance(d, dict) else None
        return d if isinstance(d, (str, int, float)) else None

    raw = (ofp.get("navlog") or {}).get("fix") if isinstance(ofp.get("navlog"), dict) else None
    if isinstance(raw, dict):
        raw = [raw]
    pts = []
    for fx in raw or []:
        try:
            ident = fx.get("ident") or ""
            if fx.get("type") in ("ltlg",) or ident in ("TOC", "TOD"):
                ident = ""
            pts.append([round(float(fx["pos_lat"]), 4), round(float(fx["pos_long"]), 4), ident[:8]])
        except (KeyError, TypeError, ValueError):
            pass
    dep_lat, dep_lon = g(ofp, "origin", "pos_lat"), g(ofp, "origin", "pos_long")
    arr_lat, arr_lon = g(ofp, "destination", "pos_lat"), g(ofp, "destination", "pos_long")
    try:
        if dep_lat:
            pts.insert(0, [round(float(dep_lat), 4), round(float(dep_lon), 4), ""])
        if arr_lat:
            pts.append([round(float(arr_lat), 4), round(float(arr_lon), 4), ""])
    except ValueError:
        pass
    while len(pts) > 400:
        mid = pts[1:-1]
        thin = [p for i, p in enumerate(mid) if p[2] or i % 2 == 0]
        if len(thin) == len(mid):
            thin = mid[::2]
        pts = pts[:1] + thin + pts[-1:]
    airline, number = g(ofp, "general", "icao_airline") or "", g(ofp, "general", "flight_number") or ""
    plan = {
        "dep": g(ofp, "origin", "icao_code"), "arr": g(ofp, "destination", "icao_code"),
        "altn": g(ofp, "alternate", "icao_code"),
        "callsign": g(ofp, "atc", "callsign") or (f"{airline}{number}" or None),
        "flight_no": f"{airline}{number}" or None,
        "route": g(ofp, "general", "route"),
        "aircraft": g(ofp, "aircraft", "icaocode"), "aircraft_name": g(ofp, "aircraft", "name"),
        "reg": g(ofp, "aircraft", "reg"),
        "cruise": g(ofp, "general", "initial_altitude"),
        "dep_rwy": g(ofp, "origin", "plan_rwy"), "arr_rwy": g(ofp, "destination", "plan_rwy"),
        "distance_nm": g(ofp, "general", "route_distance"),
        "fuel_ramp": g(ofp, "fuel", "plan_ramp"), "units": g(ofp, "params", "units"),
        "ete_min": None,
        "fixes": pts,
    }
    try:
        plan["ete_min"] = round(int(g(ofp, "times", "est_time_enroute")) / 60)
    except (TypeError, ValueError):
        pass
    for key in ("sched_out", "sched_off", "sched_on", "sched_in"):
        try:
            plan[key] = datetime.fromtimestamp(int(g(ofp, "times", key)), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            plan[key] = None
    if not plan["dep"] or not plan["arr"]:
        return None, "V SimBriefu jsem nenašel žádný letový plán."
    return plan, None


INFO_KEYS = ("callsign", "flight_no", "dep", "arr", "altn")


def _rebuild_plan():
    sb = state.get("sb_plan")
    info = {k: v for k, v in (state.get("flight_info") or {}).items() if v}
    if not sb and not info:
        plan = None
    else:
        plan = dict(sb) if sb else {"fixes": []}
        plan.update(info)
    state["plan"] = plan
    state["plan_ver"] += 1


def set_plan(plan):
    """Nový plán ze SimBriefu (ruční úpravy se tím vynulují)."""
    with lock:
        state["sb_plan"] = plan
        state["flight_info"] = {}
        _rebuild_plan()
    if plan:
        log(f"Letový plán: {plan['dep']} → {plan['arr']}" + (f" (alt. {plan['altn']})" if plan.get("altn") else ""))


def set_flight_info(info):
    """Ručně zadané údaje o letu – mají přednost před SimBriefem."""
    clean = {}
    for k in INFO_KEYS:
        v = "".join(ch for ch in str((info or {}).get(k) or "").upper() if ch.isalnum())
        clean[k] = v[:10] or None
    with lock:
        state["flight_info"] = clean
        _rebuild_plan()


# ---------------- Crew center Jáchym Air (phpVMS) ----------------
class Crew:
    """Propojení s crew centrem (phpVMS 7). API klíč zůstává jen v tomto počítači."""

    def __init__(self):
        self.key, self.auto, self.ident, self.name, self.prefs = None, True, None, None, {}
        try:
            with open(CREW_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.key, self.auto = d.get("api_key"), d.get("auto", True)
            self.ident, self.name = d.get("ident"), d.get("name")
            self.prefs = d.get("prefs") or {}
        except Exception:
            pass
        self._sync()

    @property
    def linked(self):
        return bool(self.key)

    def _sync(self, status=None):
        with lock:
            c = state["crew"]
            c.update({"linked": self.linked, "ident": self.ident, "name": self.name, "auto": self.auto})
            if status is not None:
                c["status"] = status

    def _save(self):
        with open(CREW_FILE, "w", encoding="utf-8") as f:
            json.dump({"api_key": self.key, "auto": self.auto, "ident": self.ident, "name": self.name, "prefs": self.prefs}, f)

    def _api(self, method, path, body=None, key=None):
        req = urllib.request.Request(CREW_URL + path, method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None)
        req.add_header("X-API-Key", key or self.key or "")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "JachymAirTracker")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"message": raw.decode("utf-8", "ignore")[:300]}
        except Exception as e:
            return 0, {"message": str(e)}

    @staticmethod
    def _err(res, code):
        if isinstance(res, dict):
            if isinstance(res.get("error"), dict):
                return res["error"].get("message") or str(res["error"])
            if res.get("errors"):
                return "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in res["errors"].items()) \
                    if isinstance(res["errors"], dict) else str(res["errors"])
            if res.get("message"):
                return res["message"]
        return f"chyba {code}"

    def link(self, key):
        key = (key or "").strip()
        if not key:
            return False, "Vlož svůj API klíč z crew centra."
        code, res = self._api("GET", "/api/user", key=key)
        if code != 200:
            return False, "Crew center klíč nepřijal: " + self._err(res, code)
        pilot = (res or {}).get("data") or {}
        self.key = key
        self.ident = pilot.get("ident") or pilot.get("pilot_id")
        self.name = pilot.get("name") or pilot.get("name_private")
        self._save()
        msg = f"Propojeno jako {self.ident or self.name}"
        if state["online"]:
            c2, r2 = cloud.call_function("jai-link", {"api_key": key})
            if c2 == 200 and (r2 or {}).get("ok"):
                msg += " · na mapě jsi teď pilot Jáchym Air ✔"
            else:
                msg += " · členství na webu se nepodařilo potvrdit: " + str((r2 or {}).get("msg") or (r2 or {}).get("message") or c2)
        self._sync(msg)
        log(msg)
        threading.Thread(target=self.refresh_bids, daemon=True).start()
        return True, msg

    def unlink(self):
        self.key = self.ident = self.name = None
        self.prefs = {}
        with lock:
            state["crew"]["bids"] = []
        try:
            os.remove(CREW_FILE)
        except OSError:
            pass
        self._sync("Odpojeno")

    def set_auto(self, auto):
        self.auto = bool(auto)
        self._save()
        self._sync()

    @staticmethod
    def _flight_of(b):
        """phpVMS 7 vrací rezervace jako seznam letů; starší verze je balí do {"flight": …}."""
        return b.get("flight") if isinstance(b.get("flight"), dict) else b

    def bids(self):
        code, res = self._api("GET", "/api/user/bids")
        if code != 200:
            return None, self._err(res, code)
        flights = [self._flight_of(b) for b in ((res or {}).get("data") or [])]
        self._publish_bids(flights)
        return flights, None

    def _publish_bids(self, flights):
        out = []
        for f in flights:
            al = f.get("airline") or {}
            out.append({"id": str(f.get("id")), "flight_number": f"{al.get('icao') or ''}{f.get('flight_number') or ''}",
                        "dep": f.get("dpt_airport_id"), "arr": f.get("arr_airport_id"),
                        "aircraft": self.prefs.get(str(f.get("id")), {}).get("reg")})
        with lock:
            state["crew"]["bids"] = out

    def refresh_bids(self):
        if self.linked:
            self.bids()

    def add_bid(self, flight_id, aircraft=None):
        code, res = self._api("PUT", "/api/user/bids", {"flight_id": flight_id})
        if code == 409:
            msg = "Tento let už máš rezervovaný."
        elif code not in (200, 201):
            return False, "Crew center rezervaci odmítl: " + self._err(res, code)
        else:
            msg = "Let rezervován ✔"
        if aircraft:
            self.prefs[str(flight_id)] = aircraft
            self._save()
        self.bids()
        log(msg)
        return True, msg

    def remove_bid(self, flight_id):
        code, res = self._api("DELETE", "/api/user/bids", {"flight_id": flight_id})
        if code not in (200, 201, 204):
            return False, "Rezervaci se nepodařilo zrušit: " + self._err(res, code)
        self.prefs.pop(str(flight_id), None)
        self._save()
        self.bids()
        return True, "Rezervace zrušena."

    def _pick_aircraft(self, flight, plan):
        """Vybere letadlo z flotily crew centra – podle registrace ze SimBriefu, jinak první vhodné."""
        reg = ((plan or {}).get("reg") or "").replace("-", "").upper()
        icao = ((plan or {}).get("aircraft") or "").upper()
        pref = self.prefs.get(str(flight.get("id"))) or {}
        candidates = []
        for sf in (flight.get("subfleet") or flight.get("subfleets") or []):
            candidates += sf.get("aircraft") or []
        if pref.get("id") is not None:
            for a in candidates:
                if str(a.get("id")) == str(pref["id"]):
                    return a
            if not candidates:
                return {"id": pref["id"], "registration": pref.get("reg")}
        if not candidates:
            code, res = self._api("GET", "/api/user/fleet")
            for sf in ((res or {}).get("data") or []) if code == 200 else []:
                candidates += sf.get("aircraft") or []
        for a in candidates:
            if reg and (a.get("registration") or "").replace("-", "").upper() == reg:
                return a
        for a in candidates:
            if icao and (a.get("icao") or "").upper() == icao:
                return a
        return candidates[0] if candidates else None

    def file_flight(self, fl, fuel_lbs=None):
        """Odešle dokončený let do crew centra jako PIREP. Vrací text stavu."""
        try:
            status = self._file(fl, fuel_lbs)
        except Exception as e:
            status = f"Chyba při odesílání do crew centra: {e}"
        self._sync(status)
        log("Crew center: " + status)
        with lock:
            if state.get("last_flight") and state["last_flight"]["flight"] is fl:
                state["last_flight"]["crew"] = status
        return status[:300]

    def _file(self, fl, fuel_lbs):
        dep, arr = fl.get("dep_icao"), fl.get("arr_icao")
        bids, err = self.bids()
        if bids is None:
            return f"Nepodařilo se načíst rezervace: {err}"
        match = None
        for f in bids:
            if f.get("dpt_airport_id") == dep and f.get("arr_airport_id") in (arr, fl.get("plan_arr")):
                match = f
                break
        if not match:
            return f"Let neodeslán – v crew centru nemáš rezervaci (bid) pro {dep or '?'} → {arr or '?'}."
        with lock:
            plan = dict(state["plan"]) if state.get("plan") else None
        ac = self._pick_aircraft(match, plan)
        if not ac:
            return "Let neodeslán – v crew centru jsem nenašel žádné letadlo pro tento let."
        pre = {
            "airline_id": match.get("airline_id") or (match.get("airline") or {}).get("id"),
            "aircraft_id": ac.get("id"), "flight_id": match.get("id"),
            "flight_number": match.get("flight_number"), "route_code": match.get("route_code"),
            "route_leg": match.get("route_leg"), "flight_type": match.get("flight_type") or "J",
            "dpt_airport_id": match.get("dpt_airport_id"), "arr_airport_id": match.get("arr_airport_id"),
            "route": fl.get("route") or match.get("route"), "source_name": "JAI Tracker",
            "planned_distance": match.get("distance", {}).get("nmi") if isinstance(match.get("distance"), dict) else match.get("distance"),
        }
        pre = {k: v for k, v in pre.items() if v not in (None, "")}
        code, res = self._api("POST", "/api/pireps/prefile", pre)
        if code not in (200, 201):
            return "Crew center odmítl let (prefile): " + self._err(res, code)
        pid = ((res or {}).get("data") or {}).get("id")
        notes = f"JAI Tracker: přistání {fl.get('score')}/10, {fl.get('fpm')} fpm, {fl.get('g')} G"
        if fl.get("rwy"):
            notes += f", RWY {fl['rwy']}"
        body = {
            "flight_time": max(1, round(fl.get("duration_min") or 0)),
            "distance": fl.get("distance_nm") or 0,
            "fuel_used": fuel_lbs or 0,
            "landing_rate": fl.get("fpm"),
            "source_name": "JAI Tracker",
            "route": fl.get("route") or None,
            "notes": notes,
        }
        body = {k: v for k, v in body.items() if v is not None}
        code, res = self._api("POST", f"/api/pireps/{pid}/file", body)
        if code not in (200, 201):
            return "Crew center odmítl let (file): " + self._err(res, code)
        self.prefs.pop(str(match.get("id")), None)
        self._save()
        threading.Thread(target=self.refresh_bids, daemon=True).start()
        return f"Let {match.get('flight_number') or ''} odeslán do crew centra ✔ (PIREP {pid})"


crew = Crew()


def upload_flight(flight, fuel_used=None, file_crew=True, sync=False):
    def send():
        if file_crew and crew.linked and crew.auto:
            flight["crew_status"] = crew.file_flight(flight, fuel_used)
        ok, msg = cloud.send_flight(flight) if state["online"] else (False, "nepřihlášen")
        if ok:
            log("   Let uložen na web ✔")
        else:
            queue_flight(flight)
            log(f"   Let se nepodařilo uložit ({msg}), zkusím to znovu později.")
    if sync:
        send()
    else:
        threading.Thread(target=send, daemon=True).start()


RECORDER = None


def flight_state():
    return RECORDER.state_name() if RECORDER else "idle"


def finish_flight(sync=False):
    return RECORDER.finish_now(sync=sync) if RECORDER else "nothing"


def on_exit():
    """Při zavření aplikace: rozpracovaný let uložit (ne zahodit)."""
    try:
        if RECORDER and RECORDER.state_name() != "idle":
            finish_flight(sync=True)
    except Exception as e:
        log("Let se při zavírání nepodařilo uložit:", e)


def clean_flight(flight):
    """Hodnoty ohlídané tak, aby je databáze vždy přijala."""
    body = {k: v for k, v in flight.items() if k not in ("local_id", "hold_until")}
    clamp = lambda v, lo, hi: None if v is None else max(lo, min(hi, v))
    body["distance_nm"] = clamp(body.get("distance_nm"), 0, 11999)
    body["duration_min"] = clamp(body.get("duration_min"), 0, 1999)
    body["g"] = clamp(body.get("g"), 0, 10)
    body["fpm"] = clamp(body.get("fpm"), -6000, 0)
    for key in ("path", "plan_fixes"):
        pts = body.get(key)
        if isinstance(pts, list):
            pts = [p for p in pts if isinstance(p, list) and len(p) >= 2 and p[0] and p[1]]
            while len(pts) > 2900:
                pts = pts[:1] + pts[1:-1:2] + pts[-1:]
            body[key] = pts
    if body.get("incomplete") is None:
        body.pop("incomplete", None)
    for key, n in (("flight_no", 10), ("rwy", 8), ("plan_arr", 8), ("plan_altn", 8), ("reg", 12), ("ac_icao", 12), ("va", 8), ("crew_status", 300)):
        if isinstance(body.get(key), str):
            body[key] = body[key][:n]
    return body


def unqueue_flight(local_id):
    if not local_id or not os.path.exists(QUEUE_FILE):
        return
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = [fl for fl in data if fl.get("local_id") != local_id]
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def queue_flight(flight):
    data = []
    try:
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        data = []
    data.append(flight)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def flush_queue():
    if not os.path.exists(QUEUE_FILE):
        return
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    now_t = time.time()
    left = [fl for fl in data if (fl.get("hold_until") or 0) > now_t or not cloud.send_flight(fl)[0]]
    if left:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(left, f, ensure_ascii=False, indent=2)
    else:
        os.remove(QUEUE_FILE)
    if len(left) < len(data):
        log(f"Odesláno {len(data) - len(left)} dříve neodeslaných letů ✔")


def live_interval(s):
    """Jak často posílat polohu (s): v cestovní hladině řidčeji, při vzletu a přistání často."""
    alt, gs, vs = s.get("alt") or 0, s.get("gs") or 0, abs(s.get("vs") or 0)
    on_ground = s.get("on_ground") or s.get("phase") in ("Na zemi", "Pojíždění") or (gs < 40 and alt < 15000 and vs < 100 and (s.get("agl") or 0) < 50)
    if on_ground:
        return 60 if gs < 1 else 20          # stání / pojíždění
    if alt < 10000:
        return 10                            # vzlet, přiblížení, přistání
    if vs < 300:
        return 60                            # cestovní let – udržování výšky
    return 20                                # stoupání / klesání ve výšce


def upload_loop():
    last_flush = 0
    sent_trail_ver, last_trail_t = -1, 0
    sent_plan_ver = -1
    last_sent, last_phase, last_landing = 0.0, None, None
    while True:
        time.sleep(2)
        if not state["online"] or state["demo"]:
            continue
        with lock:
            s = dict(state)
        if s["connected"]:
            now = time.time()
            interval = live_interval(s)
            changed = s.get("phase") != last_phase or bool(s.get("live_landing")) != bool(last_landing) or s["plan_ver"] != sent_plan_ver
            if changed or now - last_sent >= interval:
                try:
                    trail_every = 120 if interval >= 60 else 30
                    with_trail = s["trail_ver"] != sent_trail_ver and (now - last_trail_t > trail_every or not s["trail"] or changed)
                    with_plan = s["plan_ver"] != sent_plan_ver
                    code, res = cloud.send_live(s, with_trail, with_plan)
                    if code in (200, 201, 204):
                        last_sent, last_phase, last_landing = now, s.get("phase"), s.get("live_landing")
                    if with_trail and code in (200, 201, 204):
                        sent_trail_ver, last_trail_t = s["trail_ver"], now
                    if with_plan and code in (200, 201, 204):
                        sent_plan_ver = s["plan_ver"]
                    if code not in (200, 201, 204):
                        log("Web odmítl polohu:", code, (res or {}).get("message"))
                except Exception as e:
                    log("Polohu se nepodařilo odeslat:", e)
        if time.time() - last_flush > 60:
            last_flush = time.time()
            try:
                flush_queue()
            except Exception:
                pass


# ---------------- Automatická věž ----------------
# Z poloh ostatních pilotů v okolí hlídá obsazenou dráhu, pořadí na přiblížení a střety při pojíždění.
TOWER_FT_PER_NM = 6076.12


def _rwy_frame(lat, lon, thr, end):
    coslat = math.cos(math.radians(thr[1]))
    ex, ey = (end[2] - thr[2]) * coslat * 111320, (end[1] - thr[1]) * 110540
    length = math.hypot(ex, ey)
    if length < 100:
        return None
    px, py = (lon - thr[2]) * coslat * 111320, (lat - thr[1]) * 110540
    along = (px * ex + py * ey) / length
    cross = (px * ey - py * ex) / length
    brg = math.degrees(math.atan2(ex, ey)) % 360
    return along, cross, length, brg


def runways_near(lat, lon):
    return [r for r in RUNWAYS if abs(r[1][1] - lat) < 0.08 and abs(r[1][2] - lon) < 0.12]


def runway_occupied_by(lat, lon, rws, margin=15):
    """Dráha (název obou konců), na které bod leží, nebo None."""
    for apt, a, b, width in rws:
        f = _rwy_frame(lat, lon, a, b)
        if not f:
            continue
        along, cross, length, _ = f
        if -60 <= along <= length + 60 and abs(cross) <= width * 0.3048 / 2 + margin:
            return f"{a[0]}/{b[0]}", apt
    return None


def runway_near_edge(lat, lon, rws, dist=120):
    """Letadlo je u dráhy (např. na čekacím místě), ale ne na ní."""
    for apt, a, b, width in rws:
        f = _rwy_frame(lat, lon, a, b)
        if not f:
            continue
        along, cross, length, _ = f
        half = width * 0.3048 / 2
        if -80 <= along <= length + 80 and half + 10 < abs(cross) <= half + dist:
            return f"{a[0]}/{b[0]}", apt
    return None


def on_final(lat, lon, hdg, agl, rws, max_nm=8):
    """Letadlo na finále: před prahem, v ose, ve správném směru, nízko. Vrací (dráha, konec, vzdálenost nm)."""
    if agl is None or agl > 3500:
        return None
    best = None
    for apt, a, b, width in rws_big(lat, lon):
        for thr, end in ((a, b), (b, a)):
            f = _rwy_frame(lat, lon, thr, end)
            if not f:
                continue
            along, cross, length, brg = f
            dist_m = -along
            if not (0 < dist_m < max_nm * 1852):
                continue
            if abs(cross) > 250 + dist_m * 0.08 or abs((hdg - brg + 180) % 360 - 180) > 25:
                continue
            nm = dist_m / 1852
            if best is None or nm < best[2]:
                best = (f"{a[0]}/{b[0]}", thr[0], nm, apt)
    return best


def rws_big(lat, lon):
    return [r for r in RUNWAYS if abs(r[1][1] - lat) < 0.25 and abs(r[1][2] - lon) < 0.35]


def _extrapolate(o, now):
    """Ostatní posílají polohu jen občas – dopočítat, kde asi jsou teď."""
    try:
        age = min(20.0, max(0.0, now - datetime.fromisoformat(str(o["updated_at"]).replace("Z", "+00:00")).timestamp()))
    except Exception:
        age = 0.0
    gs, hdg = o.get("gs") or 0, math.radians(o.get("hdg") or 0)
    d_m = gs * 0.514444 * age
    lat = o["lat"] + d_m * math.cos(hdg) / 110540
    lon = o["lon"] + d_m * math.sin(hdg) / (111320 * math.cos(math.radians(o["lat"])))
    return lat, lon


NATO = {"A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot", "G": "Golf", "H": "Hotel", "I": "India",
        "J": "Juliett", "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November", "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo",
        "S": "Sierra", "T": "Tango", "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu"}
DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "niner"]
TELEPHONY = {"JAI": "Algeria", "JAE": "Eurosky", "TVS": "Skytravel", "SWG": "Sunwing", "RYR": "Ryanair", "WZZ": "Wizz Air", "DLH": "Lufthansa", "EZY": "Easy",
             "CSA": "C-S-A", "AFR": "Air France", "BAW": "Speedbird", "KLM": "K-L-M", "UAE": "Emirates", "THY": "Turkish", "DAH": "Air Algerie"}


def say_digits(txt):
    return " ".join(DIGITS[int(c)] if c.isdigit() else NATO.get(c.upper(), c) for c in str(txt) if c.strip())


def say_callsign(cs):
    """Callsign tak, jak ho řekne věž: JAI127 -> Jachym one two seven."""
    cs = (cs or "").upper().replace(" ", "").replace("-", "")
    m = re.match(r"^([A-Z]{3})(\d.*)$", cs)
    if m:
        return f"{TELEPHONY.get(m.group(1), say_digits(m.group(1)))} {say_digits(m.group(2))}"
    return say_digits(cs)


def say_runway(rw):
    m = re.match(r"^(\d{1,2})([LRC]?)$", str(rw or "").upper())
    if not m:
        return say_digits(rw)
    side = {"L": " left", "R": " right", "C": " center"}.get(m.group(2), "")
    return say_digits(m.group(1).zfill(2)) + side


def say_wind(s):
    d, k = s.get("wind_dir"), s.get("wind_kt")
    if d is None or k is None:
        return ""
    if k < 3:
        return "wind calm, "
    return f"wind {say_digits(str(int(round(d / 10) * 10) % 360 or 360).zfill(3))} degrees, {int(round(k))} knots, "


def runway_end_aligned(lat, lon, hdg, rws):
    """Na které dráze stojím a kterým směrem (konec, ze kterého vzlétám)."""
    for apt, a, b, width in rws:
        for thr, end in ((a, b), (b, a)):
            f = _rwy_frame(lat, lon, thr, end)
            if not f:
                continue
            along, cross, length, brg = f
            if -60 <= along <= length + 60 and abs(cross) <= width * 0.3048 / 2 + 15 and abs((hdg - brg + 180) % 360 - 180) < 30:
                return thr[0], f"{a[0]}/{b[0]}", apt
    return None


def tower_alerts(s, others):
    """Seznam upozornění pro mé letadlo. Úrovně: danger (hned reagovat), warn, info."""
    alerts = []
    lat, lon, hdg, gs = s.get("lat"), s.get("lon"), s.get("hdg") or 0, s.get("gs") or 0
    agl = s.get("agl") or 0
    if lat is None or not RUNWAYS:
        return alerts
    elev = (s.get("alt") or 0) - agl                     # nadmořská výška terénu pod námi ≈ letiště
    on_ground = bool(s.get("on_ground"))
    rws = runways_near(lat, lon)
    now = time.time()
    traffic = []
    for o in others:
        if o.get("lat") is None:
            continue
        olat, olon = _extrapolate(o, now)
        cs = o.get("plan_cs") or o.get("callsign") or "?"
        o_agl = max(0, (o.get("alt") or 0) - elev)
        o_ground = o.get("phase") in ("Na zemi", "Pojíždění", "Vzlet") and o_agl < 200 or ((o.get("gs") or 0) < 40 and o_agl < 150)
        traffic.append({"cs": cs, "lat": olat, "lon": olon, "agl": o_agl, "ground": o_ground, "gs": o.get("gs") or 0, "hdg": o.get("hdg") or 0, "arr": o.get("plan_arr")})

    def occ_text(rwy):
        for t in traffic:
            if t["ground"]:
                hit = runway_occupied_by(t["lat"], t["lon"], rws)
                if hit and hit[0] == rwy:
                    return t["cs"], "na dráze"
        return None

    def finals(rwy_pair):
        out = []
        for t in traffic:
            if t["ground"]:
                continue
            f = on_final(t["lat"], t["lon"], t["hdg"], t["agl"], rws)
            if f and f[0] == rwy_pair:
                out.append((f[2], t["cs"], f[1]))
        return sorted(out)

    me = say_callsign(((s.get("plan") or {}).get("callsign")) or s.get("callsign"))
    if on_ground:
        mine = runway_occupied_by(lat, lon, rws)
        if mine:
            occ = occ_text(mine[0])
            fin = finals(mine[0])
            aligned = runway_end_aligned(lat, lon, hdg, rws)
            if occ:
                alerts.append({"level": "danger" if gs > 30 else "warn", "text": f"Dráha {mine[0]} v {mine[1]} je obsazená – {occ[0]} {occ[1]}.",
                               "sid": f"occ-{mine[0]}-{occ[0]}", "say": f"{me}, hold position, runway occupied."})
            if fin and fin[0][0] < 5:
                d, cs, end = fin[0]
                lvl = "danger" if gs > 30 or d < 2.5 else "warn"
                alerts.append({"level": lvl, "text": f"{cs} na finále RWY {end}, {d:.1f} nm – {'pozor při vzletu!' if gs > 30 else 'uvolni dráhu.'}",
                               "sid": f"fin-{end}-{cs}-{'roll' if gs > 30 else 'rw'}",
                               "say": f"{me}, stop immediately, I say again, stop immediately, traffic on short final." if gs > 30 and gs < 110
                               else f"{me}, vacate runway without delay, traffic on final."})
            # povolení ke vzletu jen před odletem – ne po přistání (backtrack, otáčení na dráze)
            just_landed = bool(s.get("live_landing")) or atc.enabled      # s ATC dává povolení ke vzletu ATC

            if aligned and not occ and not (fin and fin[0][0] < 5) and gs < 40 and not just_landed:
                alerts.append({"level": "ok", "text": f"Dráha {aligned[0]} v {aligned[2]} je volná – můžeš vzlétnout.",
                               "sid": f"to-{aligned[0]}", "say": f"{me}, {say_wind(s)}runway {say_runway(aligned[0])}, cleared for takeoff."})
        else:
            edge = runway_near_edge(lat, lon, rws)
            if edge:
                occ = occ_text(edge[0])
                fin = finals(edge[0])
                if occ:
                    alerts.append({"level": "warn", "text": f"Čekej před dráhou {edge[0]}: {occ[0]} je na dráze.",
                                   "sid": f"hs-{edge[0]}-{occ[0]}", "say": f"{me}, hold short of the runway, traffic on the runway."})
                elif fin and fin[0][0] < 6:
                    alerts.append({"level": "warn", "text": f"Čekej před dráhou {edge[0]}: {fin[0][1]} na finále RWY {fin[0][2]}, {fin[0][0]:.1f} nm.",
                                   "sid": f"hs-{edge[0]}-{fin[0][1]}", "say": f"{me}, hold short runway {say_runway(fin[0][2])}, traffic on final."})
                else:
                    alerts.append({"level": "ok", "text": f"Dráha {edge[0]} je volná."})
            # střet při pojíždění
            if gs > 3:
                for t in traffic:
                    if not t["ground"]:
                        continue
                    dn = (t["lat"] - lat) * 110540
                    de = (t["lon"] - lon) * 111320 * math.cos(math.radians(lat))
                    d = math.hypot(dn, de)
                    ahead = abs((math.degrees(math.atan2(de, dn)) % 360 - hdg + 180) % 360 - 180) < 50
                    if d < 160 and ahead:
                        alerts.append({"level": "warn", "text": f"Pozor, {t['cs']} je {round(d / 10) * 10} m před tebou.",
                                       "sid": f"tx-{t['cs']}", "say": f"{me}, caution, traffic ahead."})
    else:
        mf = on_final(lat, lon, hdg, agl, rws)
        if mf:
            pair, end, d, apt = mf
            occ = occ_text(pair)
            if occ:
                ga = d < 2.5
                alerts.append({"level": "danger" if ga else "warn", "text": f"RWY {end} v {apt} je obsazená – {occ[0]} {occ[1]}." + (" Připrav se na opakování přiblížení!" if ga else ""),
                               "sid": f"{'ga' if ga else 'cont'}-{end}", "say": f"{me}, go around, I say again, go around, runway occupied." if ga
                               else f"{me}, continue approach, runway occupied, expect late landing clearance."})
            ahead = [x for x in finals(pair) if x[0] < d]
            if ahead:
                alerts.append({"level": "info", "text": f"Jsi číslo {len(ahead) + 1} na finále RWY {end}, před tebou {ahead[-1][1]} ({d - ahead[-1][0]:.1f} nm).",
                               "sid": f"seq-{end}-{len(ahead) + 1}", "say": f"{me}, number {len(ahead) + 1}, follow {say_callsign(ahead[-1][1])} on final."})
            elif not occ:
                alerts.append({"level": "ok", "text": f"Dráha {end} v {apt} je volná." + (" Povoleno přistání." if d <= 4.5 else ""),
                               **({"sid": f"ctl-{end}", "say": f"{me}, {say_wind(s)}runway {say_runway(end)}, cleared to land."} if d <= 4.5 else {})})
        else:
            # pořadí na přiblížení do cíle podle vzdálenosti
            arr = ((s.get("plan") or {}).get("arr") or "").upper()
            ap = next((a for a in AIRPORTS if a[0] == arr), None) if arr else None
            if ap and agl < 15000:
                my_d = haversine_nm(lat, lon, ap[2], ap[3])
                if my_d < 40:
                    ahead = sorted((haversine_nm(t["lat"], t["lon"], ap[2], ap[3]), t["cs"]) for t in traffic
                                   if not t["ground"] and (t["arr"] or "").upper() == arr and haversine_nm(t["lat"], t["lon"], ap[2], ap[3]) < my_d)
                    if ahead:
                        alerts.append({"level": "info", "text": f"Jsi číslo {len(ahead) + 1} na přiblížení do {arr}, před tebou {ahead[-1][1]} ({my_d - ahead[-1][0]:.0f} nm)."})
    return alerts


# ---------------- Discord Rich Presence („Hraje Jachym Air Tracker“) ----------------
# Vlastní malý klient (bez dalších knihoven): pojmenovaná roura Discordu, handshake a SET_ACTIVITY.
DISCORD_APP_ID = "1570732052268064809"          # Application ID z discord.com/developers (aplikace „Jachym Air Tracker“)
discord_state = {"enabled": True, "connected": False, "error": None}
PHASE_EN = {"Na zemi": "Parked", "Pojíždění": "Taxi", "Vzlet": "Takeoff", "Stoupání": "Climb", "Cestovní let": "Cruise",
            "Klesání": "Descent", "Přiblížení": "Approach", "Přistání": "Landing"}


class DiscordIPC:
    def __init__(self, app_id):
        self.app_id, self.pipe = app_id, None

    def _send(self, op, payload):
        data = json.dumps(payload).encode("utf-8")
        self.pipe.write(struct.pack("<II", op, len(data)) + data)
        self.pipe.flush()
        hdr = self.pipe.read(8)
        if len(hdr) < 8:
            raise OSError("Discord neodpověděl")
        _, ln = struct.unpack("<II", hdr)
        return json.loads(self.pipe.read(ln).decode("utf-8") or "{}")

    def connect(self):
        for i in range(10):
            try:
                self.pipe = open(rf"\\?\pipe\discord-ipc-{i}", "r+b", buffering=0)
                break
            except OSError:
                continue
        if not self.pipe:
            raise OSError("Discord neběží")
        self._send(0, {"v": 1, "client_id": self.app_id})

    def set_activity(self, activity):
        return self._send(1, {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": activity}, "nonce": str(time.time())})

    def close(self):
        try:
            self.pipe and self.pipe.close()
        except Exception:
            pass
        self.pipe = None


def presence_activity(s):
    plan = s.get("plan") or {}
    cs = plan.get("callsign") or s.get("callsign") or ""
    dep, arr = plan.get("dep") or s.get("dep") or "", plan.get("arr") or ""
    phase = PHASE_EN.get(s.get("phase"), s.get("phase") or "")
    alt = s.get("alt") or 0
    state = phase
    if phase in ("Cruise", "Climb", "Descent") and alt > 10000:
        state = f"{phase} · FL{round(alt / 100):03d}"
    elif phase and alt > 0 and phase not in ("Parked", "Taxi"):
        state = f"{phase} · {round(alt / 100) * 100:,} ft".replace(",", " ")
    details = " · ".join(x for x in [cs, f"{dep} → {arr}" if dep and arr else dep] if x) or "Na zemi"
    act = {"details": details[:128], "state": (state or "Connected")[:128],
           "assets": {"large_image": "logo", "large_text": "Jachym Air Tracker", "small_text": (s.get("aircraft") or "")[:128]},
           "buttons": [{"label": "Sledovat na mapě", "url": "https://tracker.jachymair.com/" + (f"#/let/live/{cloud.user_id}" if getattr(cloud, "user_id", None) else "")}]}
    to = (rec_ref.get("rec").takeoff or {}) if rec_ref.get("rec") else {}
    if to.get("wall") and not to.get("landed"):
        act["timestamps"] = {"start": int(to["wall"])}
    return act


rec_ref = {}


def discord_loop():
    if not DISCORD_APP_ID:
        return
    ipc, last = None, None
    while True:
        time.sleep(15)
        try:
            with lock:
                s = dict(state)
            if not discord_state["enabled"] or not s.get("connected"):
                if ipc:
                    ipc.set_activity(None)
                    ipc.close()
                    ipc, last = None, None
                    discord_state["connected"] = False
                continue
            if ipc is None:
                ipc = DiscordIPC(DISCORD_APP_ID)
                ipc.connect()
                discord_state.update(connected=True, error=None)
            act = presence_activity(s)
            key = json.dumps(act, sort_keys=True)
            if key != last:
                ipc.set_activity(act)
                last = key
        except Exception as e:
            discord_state.update(connected=False, error=str(e)[:120])
            if ipc:
                ipc.close()
            ipc, last = None, None


# ---------------- Data letišť ze simulátoru (SimConnect facility data) ----------------
# Diagnostika pro ATC fázi 2: frekvence, SID, STAR, přiblížení a pojezdové dráhy přímo z MSFS.
FAC_TYPES = {0: "AIRPORT", 1: "RUNWAY", 2: "START", 3: "FREQUENCY", 4: "HELIPAD", 5: "APPROACH", 6: "APPROACH_TRANSITION", 7: "APPROACH_LEG",
             8: "FINAL_APPROACH_LEG", 9: "MISSED_APPROACH_LEG", 10: "DEPARTURE", 11: "ARRIVAL", 12: "RUNWAY_TRANSITION", 13: "ENROUTE_TRANSITION",
             14: "TAXI_POINT", 15: "TAXI_PARKING", 16: "TAXI_PATH", 17: "TAXI_NAME", 18: "JETWAY"}
FREQ_TYPES = {0: "NONE", 1: "ATIS", 2: "MULTICOM", 3: "UNICOM", 4: "CTAF", 5: "GROUND", 6: "TOWER", 7: "CLEARANCE", 8: "APPROACH",
              9: "DEPARTURE", 10: "CENTER", 11: "FSS", 12: "AWOS", 13: "ASOS", 14: "CPT", 15: "GCO"}
# pořadí a typy polí, které si o každém objektu vyžádáme (formát pro struct)
FAC_DEF = [
    ("AIRPORT", [("ICAO", "8s"), ("NAME", "32s"), ("LATITUDE", "d"), ("LONGITUDE", "d"), ("ALTITUDE", "d"),
                 ("N_RUNWAYS", "i"), ("N_FREQUENCIES", "i"), ("N_DEPARTURES", "i"), ("N_ARRIVALS", "i"), ("N_APPROACHES", "i"), ("N_TAXI_NAMES", "i")]),
    ("FREQUENCY", [("TYPE", "i"), ("FREQUENCY", "i"), ("NAME", "64s")]),
    ("DEPARTURE", [("NAME", "8s")]),
    ("ARRIVAL", [("NAME", "8s")]),
    ("APPROACH", [("TYPE", "i"), ("SUFFIX", "i"), ("RUNWAY_NUMBER", "i"), ("RUNWAY_DESIGNATOR", "i")]),
    ("TAXI_NAME", [("NAME", "32s")]),
]
FAC_DEF_ID, FAC_REQ_BASE = 7700, 7700
FAC_PENDING = []          # ICAO čekající na dotaz (zpracuje smyčka simulátoru)
fac_result = {}           # poslední diagnostika (pro kartu v aplikaci)
FAC_CACHE = {}            # ICAO -> {"status", "items"} – data letišť pro ATC
FAC_REQS = {}             # id dotazu -> ICAO
_fac_defined = set()


def fac_request(icao, diag=True):
    icao = (icao or "").upper().strip()
    if not re.match(r"^[A-Z0-9]{3,4}$", icao):
        return False
    if diag:
        dll = fac_result.get("dll")
        fac_result.clear()
        fac_result.update(dll=dll, icao=icao, status="čekám na simulátor", t=time.time(), errors=[], items={}, raw={})
    elif icao in FAC_CACHE:
        return True
    FAC_CACHE[icao] = {"status": "čekám", "items": {}}
    FAC_PENDING.append(icao)
    return True


def atc_freq(icao, kind):
    """Frekvence letiště ze simulátoru: (Hz, název) nebo None. kind: delivery/ground/tower/departure/approach."""
    items = (FAC_CACHE.get((icao or "").upper()) or {}).get("items", {}).get("FREQUENCY") or []
    order = {"delivery": ["CLEARANCE", "CPT", "GROUND"], "ground": ["GROUND"], "tower": ["TOWER", "CTAF"],
             "departure": ["DEPARTURE", "APPROACH"], "approach": ["APPROACH"]}.get(kind, [])
    for t in order:
        for f in items:
            if f.get("TYPE_NAME") == t and f.get("FREQUENCY"):
                return f["FREQUENCY"], (f.get("NAME") or "").strip()
    return None


def say_freq(hz):
    mhz = f"{hz / 1e6:.3f}".rstrip("0")
    if mhz.endswith("."):
        mhz += "0"
    a, b = mhz.split(".")
    return f"{say_digits(a)} decimal {say_digits(b)}"


def fmt_freq(hz):
    return f"{hz / 1e6:.3f}"


def _fac_define(sm):
    """Definice polí pro dotaz (jednou za připojení)."""
    if id(sm) in _fac_defined:
        return
    f = sm.dll.SimConnect.SimConnect_AddToFacilityDefinition
    names = []
    for obj, fields in FAC_DEF:
        if obj != "AIRPORT":
            names.append(f"OPEN {obj}")
        else:
            names.append("OPEN AIRPORT")
        for name, _ in fields:
            names.append(name)
        if obj != "AIRPORT":
            names.append(f"CLOSE {obj}")
    names.append("CLOSE AIRPORT")
    fac_result["sent_fields"] = names
    for n in names:
        f(sm.hSimConnect, ctypes.c_uint(FAC_DEF_ID), ctypes.c_char_p(n.encode()))
    _fac_defined.add(id(sm))


def _fac_parse(type_id, block):
    """Rozbalí jeden objekt podle naší definice."""
    name = FAC_TYPES.get(type_id)
    fields = dict(FAC_DEF).get(name)
    if not fields:
        return None
    out, off = {}, 0
    for fname, fmt in fields:
        size = struct.calcsize("<" + fmt)
        if off + size > len(block):
            break
        v = struct.unpack_from("<" + fmt, block, off)[0]
        if isinstance(v, bytes):
            v = v.split(b"\0", 1)[0].decode("latin-1", "replace")
        out[fname] = v
        off += size
    if name == "FREQUENCY":
        out["TYPE_NAME"] = FREQ_TYPES.get(out.get("TYPE"), out.get("TYPE"))
    return name, out


def fac_on_recv(raw):
    """Zpráva ze simulátoru (už jako bajty): 28 = data, 29 = konec, 1 = chyba."""
    try:
        dw_id = struct.unpack_from("<I", raw, 8)[0]
        if dw_id == 1:            # SIMCONNECT_RECV_EXCEPTION: dwException, dwSendID, dwIndex
            exc, send_id, idx = struct.unpack_from("<III", raw, 12)
            fac_result.setdefault("errors", []).append({"exception": exc, "send_id": send_id, "index": idx})
            return
        user_req, uniq, parent, typ, is_list, item_idx, list_size = struct.unpack_from("<7I", raw, 12)
        icao = FAC_REQS.get(user_req)
        diag = icao is not None and icao == fac_result.get("icao")
        if dw_id == 29:
            if icao in FAC_CACHE:
                FAC_CACHE[icao]["status"] = "hotovo"
            if diag:
                fac_result["status"] = "hotovo"
            return
        if dw_id == 28 and icao in FAC_CACHE:
            parsed0 = _fac_parse(typ, raw[40:])
            if parsed0:
                FAC_CACHE[icao]["items"].setdefault(parsed0[0], []).append(parsed0[1])
        if not diag:
            return
        block = raw[40:]
        tname = FAC_TYPES.get(typ, str(typ))
        raw_s = fac_result.setdefault("raw", {})
        if tname not in raw_s:
            raw_s[tname] = block[:96].hex()           # vzorek pro ladění
        parsed = _fac_parse(typ, block)
        if parsed:
            fac_result.setdefault("items", {}).setdefault(parsed[0], []).append(parsed[1])
        fac_result["status"] = "přijímám data"
    except Exception as e:
        fac_result.setdefault("errors", []).append({"parse": str(e)[:120]})


def find_new_simconnect_dll():
    """Novější SimConnect.dll (MSFS SDK) – umí data letišť. Přibalený v aplikaci, nebo z nainstalovaného SDK."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    cands = [os.path.join(base, "simconnect", "SimConnect.dll"), os.path.join(os.path.dirname(sys.executable), "SimConnect.dll")]
    for env in ("MSFS2024_SDK", "MSFS_SDK"):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "SimConnect SDK", "lib", "SimConnect.dll"))
    cands += [r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll", r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll"]
    for p in cands:
        try:
            if os.path.exists(p):
                d = ctypes.WinDLL(p)
                getattr(d, "SimConnect_RequestFacilityData")          # má potřebnou funkci?
                return p
        except Exception:
            continue
    return None


def fac_make_simconnect(SimConnectCls):
    """Podtřída knihovny SimConnect, která navíc zachytí data letišť."""
    class JaiSimConnect(SimConnectCls):
        def my_dispatch_proc(self, pData, cbData, pContext):
            try:
                dw_id = pData.contents.dwID
                if dw_id in (28, 29) or (dw_id == 1 and fac_result.get("status") not in (None, "hotovo")):
                    fac_on_recv(ctypes.string_at(pData, cbData))
                    if dw_id != 1:
                        return
            except Exception:
                pass
            return super().my_dispatch_proc(pData, cbData, pContext)
    return JaiSimConnect


def fac_process(sm):
    """Volá smyčka simulátoru: odešle čekající dotazy."""
    while FAC_PENDING:
        icao = FAC_PENDING.pop(0)
        if not hasattr(sm.dll.SimConnect, "SimConnect_RequestFacilityData"):
            if icao == fac_result.get("icao"):
                fac_result.update(status="chyba", error="Chybí novější SimConnect.dll z MSFS SDK (starý neumí data letišť).")
            FAC_CACHE.pop(icao, None)
            continue
        try:
            _fac_define(sm)
            req_id = FAC_REQ_BASE + 1 + len(FAC_REQS)
            FAC_REQS[req_id] = icao
            r = sm.dll.SimConnect.SimConnect_RequestFacilityData(sm.hSimConnect, ctypes.c_uint(FAC_DEF_ID), ctypes.c_uint(req_id),
                                                                   ctypes.c_char_p(icao.encode()), ctypes.c_char_p(b""))
            if icao == fac_result.get("icao"):
                fac_result["status"] = "dotaz odeslán" + ("" if r == 0 else f" (návratový kód {r})")
        except Exception as e:
            fac_result.update(status="chyba", error=str(e)[:200])


# ---------------- Hlasy Piper (přirozené hlasy, offline) ----------------
# Stahují se až na vyžádání do složky aplikace. Piper (MIT) + hlasy z rhasspy/piper-voices.
PIPER_DIR = os.path.join(DATA_DIR, "piper")
PIPER_ZIP_URL = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
PIPER_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
# hlas -> (soubor, cesta na Hugging Face, velikost MB)
VOICE_FILES = {
    "north": ("en_GB-northern_english_male-medium", "en/en_GB/northern_english_male/medium", 63),
    "south": ("en_GB-southern_english_female-low", "en/en_GB/southern_english_female/low", 20),
    "alba": ("en_GB-alba-medium", "en/en_GB/alba/medium", 63),
    "vctk": ("en_GB-vctk-medium", "en/en_GB/vctk/medium", 77),
    # experimentální místní přízvuk (cizí hlas čte anglický text)
    "es": ("es_ES-sharvard-medium", "es/es_ES/sharvard/medium", 63),
    "de": ("de_DE-thorsten-medium", "de/de_DE/thorsten/medium", 63),
    "fr": ("fr_FR-siwis-medium", "fr/fr_FR/siwis/medium", 63),
}
BASE_VOICES = ("north", "south", "alba", "vctk")
# přízvuk -> role -> (hlas, mluvčí)  (mluvčí VCTK podle jejich přízvuku)
ACCENTS = {
    "uk": {"tower": ("north", None), "ground": ("south", None), "radar": ("alba", None)},
    "sco": {"tower": ("vctk", "p237"), "ground": ("vctk", "p234"), "radar": ("alba", None)},
    "ie": {"tower": ("vctk", "p245"), "ground": ("vctk", "p261"), "radar": ("vctk", "p245")},
    "us": {"tower": ("vctk", "p311"), "ground": ("vctk", "p294"), "radar": ("vctk", "p360")},
    "ca": {"tower": ("vctk", "p316"), "ground": ("vctk", "p294"), "radar": ("vctk", "p302")},
    "in": {"tower": ("vctk", "p251"), "ground": ("vctk", "p248"), "radar": ("vctk", "p376")},
    "za": {"tower": ("vctk", "p347"), "ground": ("vctk", "p314"), "radar": ("vctk", "p347")},
    "au": {"tower": ("vctk", "p374"), "ground": ("vctk", "p335"), "radar": ("vctk", "p374")},
    "es": {"tower": ("es", 0), "ground": ("es", 1), "radar": ("es", 0)},
    "de": {"tower": ("de", None), "ground": ("de", None), "radar": ("de", None)},
    "fr": {"tower": ("fr", None), "ground": ("fr", None), "radar": ("fr", None)},
}
PILOT_VOICE = ("vctk", "p226")
LOCAL_ACCENT = {"LE": "es", "GC": "es", "ED": "de", "ET": "de", "LO": "de", "LS": "de", "LF": "fr", "DA": "fr", "DT": "fr", "GM": "fr"}


def accent_for(icao, mode):
    """Přízvuk podle země letiště. mode: off = britský všude, native = rodilé anglické přízvuky, local = i místní (experimentální)."""
    icao = (icao or "").upper()
    if mode == "off" or not icao:
        return "uk"
    if mode in ("local", "native") and icao[:2] in LOCAL_ACCENT:
        return LOCAL_ACCENT[icao[:2]]
    if icao.startswith(("EGP", "EGE")):
        return "sco"
    if icao.startswith(("EI", "EGA")):
        return "ie"
    if icao[:1] in ("K", "P"):
        return "us"
    if icao[:1] == "C":
        return "ca"
    if icao[:2] in ("VA", "VE", "VI", "VO"):
        return "in"
    if icao[:2] == "FA":
        return "za"
    if icao[:1] == "Y" or icao[:2] == "NZ":
        return "au"
    return "uk"
piper_state = {"installed": False, "busy": False, "progress": 0, "step": "", "error": None}
_piper_q = []
_piper_lock = threading.Lock()


def _piper_exe():
    for root, dirs, files in os.walk(PIPER_DIR):
        if "piper.exe" in files:
            return os.path.join(root, "piper.exe")
    return None


def _voice_ok(vid):
    return os.path.exists(os.path.join(PIPER_DIR, VOICE_FILES[vid][0] + ".onnx"))


def piper_check():
    ok = bool(_piper_exe()) and all(_voice_ok(v) for v in BASE_VOICES)
    piper_state["installed"] = ok
    piper_state["custom"] = custom_voices()
    return ok


CUSTOM_DIR = os.path.join(PIPER_DIR, "vlastni")


def custom_voices():
    """Vlastní hlasy (např. natrénovaný hlas pilota) – soubory .onnx + .onnx.json ve složce vlastni."""
    try:
        return sorted(f[:-5] for f in os.listdir(CUSTOM_DIR) if f.endswith(".onnx") and os.path.exists(os.path.join(CUSTOM_DIR, f + ".json")))
    except Exception:
        return []


_spk_cache = {}


def _speaker_id(vid, spk):
    if spk is None or isinstance(spk, int):
        return spk
    if vid not in _spk_cache:
        try:
            with open(os.path.join(PIPER_DIR, VOICE_FILES[vid][0] + ".onnx.json"), encoding="utf-8") as f:
                _spk_cache[vid] = json.load(f).get("speaker_id_map") or {}
        except Exception:
            _spk_cache[vid] = {}
    return _spk_cache[vid].get(spk)


_downloading = set()


def _fetch_voice(vid):
    """Stáhne jeden hlas (na pozadí), např. pro experimentální místní přízvuk."""
    if vid in _downloading or _voice_ok(vid):
        return
    _downloading.add(vid)
    def run():
        name, hf, size = VOICE_FILES[vid]
        try:
            for ext in (".onnx", ".onnx.json"):
                p = os.path.join(PIPER_DIR, name + ext)
                if not os.path.exists(p):
                    _download(f"{PIPER_HF}{hf}/{name}{ext}?download=true", p, 0, size)
            try:
                _download(f"{PIPER_HF}{hf}/MODEL_CARD?download=true", os.path.join(PIPER_DIR, name + ".MODEL_CARD.txt"), 0, 1)
            except Exception:
                pass
            log(f"Hlas {name} stažen ✔")
        except Exception as e:
            log("Hlas se nepodařilo stáhnout:", name, e)
        finally:
            _downloading.discard(vid)
    threading.Thread(target=run, daemon=True).start()


def _download(url, path, done_mb, total_mb):
    tmp = path + ".part"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "JAI-Tracker"}), timeout=60) as r, open(tmp, "wb") as f:
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            piper_state["progress"] = min(99, int((done_mb + got / 1e6) / total_mb * 100))
    os.replace(tmp, path)


def piper_install():
    """Stáhne Piper a hlasy (~250 MB) na pozadí."""
    if piper_state["busy"]:
        return
    def run():
        import zipfile
        piper_state.update(busy=True, error=None, progress=0)
        try:
            os.makedirs(PIPER_DIR, exist_ok=True)
            total, done = 20 + sum(VOICE_FILES[v][2] for v in BASE_VOICES), 0
            if not _piper_exe():
                piper_state["step"] = "program Piper"
                zp = os.path.join(PIPER_DIR, "piper.zip")
                _download(PIPER_ZIP_URL, zp, done, total)
                with zipfile.ZipFile(zp) as z:
                    z.extractall(PIPER_DIR)
                os.remove(zp)
            done += 20
            for vid in BASE_VOICES:
                name, hf, size = VOICE_FILES[vid]
                piper_state["step"] = f"hlas {name}"
                for ext in (".onnx", ".onnx.json"):
                    p = os.path.join(PIPER_DIR, name + ext)
                    if not os.path.exists(p):
                        _download(f"{PIPER_HF}{hf}/{name}{ext}?download=true", p, done, total)
                card = os.path.join(PIPER_DIR, name + ".MODEL_CARD.txt")       # licence a autoři hlasu
                if not os.path.exists(card):
                    try:
                        _download(f"{PIPER_HF}{hf}/MODEL_CARD?download=true", card, done, total)
                    except Exception:
                        pass
                done += size
            piper_state.update(progress=100, step="hotovo")
            log("Hlasy Piper připravené ✔")
        except Exception as e:
            piper_state["error"] = str(e)[:200]
            log("Hlasy Piper se nepodařilo stáhnout:", e)
        finally:
            piper_state["busy"] = False
            piper_check()
    threading.Thread(target=run, daemon=True).start()


def _radio_fx(path):
    """Jednoduchý „rádiový“ zvuk: pásmová propust 300–3400 Hz a lehké zkreslení."""
    import wave, array
    try:
        with wave.open(path, "rb") as w:
            params = w.getparams()
            data = array.array("h", w.readframes(w.getnframes()))
        sr = params.framerate
        a_hp = math.exp(-2 * math.pi * 300 / sr)
        a_lp = math.exp(-2 * math.pi * 3400 / sr)
        prev_x = prev_hp = lp = 0.0
        out = array.array("h")
        for x in data:
            hp = a_hp * (prev_hp + x - prev_x)
            prev_x, prev_hp = x, hp
            lp = lp + (1 - a_lp) * (hp - lp)
            v = max(-1.0, min(1.0, lp * 1.6 / 32768))
            v = math.tanh(v * 1.4) + random.uniform(-0.006, 0.006)
            out.append(int(max(-32767, min(32767, v * 30000))))
        with wave.open(path, "wb") as w:
            w.setparams(params)
            w.writeframes(out.tobytes())
    except Exception:
        pass


def _piper_worker():
    import subprocess, tempfile
    while True:
        with _piper_lock:
            job = _piper_q.pop(0) if _piper_q else None
        if not job:
            time.sleep(0.15)
            continue
        text, role, speed, radio, icao = job
        exe = _piper_exe()
        model, spk = resolve_voice(role, icao)
        if not exe or not model or not os.path.exists(model):
            continue
        vkey = (icao, role, os.path.basename(model), spk)
        if vkey != getattr(_piper_worker, "last", None):
            _piper_worker.last = vkey
            log(f"🔊 Hlas pro {icao or 'trať'} ({role}): {os.path.basename(model)[:-5]}{'' if spk is None else ' #' + str(spk)}")
        out = os.path.join(tempfile.gettempdir(), f"jai_atc_{int(time.time() * 1000)}.wav")
        args = [exe, "--model", model, "--output_file", out, "--length_scale", str(speed)]
        if spk is not None:
            args += ["--speaker", str(spk)]
        try:
            subprocess.run(args, input=text.encode("utf-8"), capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if radio:
                _radio_fx(out)
            import winsound
            winsound.PlaySound(out, winsound.SND_FILENAME)
        except Exception as e:
            log("Hlas Piper selhal:", e)
        finally:
            try:
                os.remove(out)
            except Exception:
                pass


VOICE_PREFS = {"accents": "native", "rules": {}}       # pravidla: {"LKPR:tower": "custom:Jachym", "LEPA:*": "accent:es"}


def resolve_voice(role, icao):
    """Vrátí (cesta k modelu, id mluvčího) pro roli a letiště."""
    icao = (icao or "").upper()
    rules = VOICE_PREFS.get("rules") or {}
    rule = rules.get(f"{icao}:{role}") or rules.get(f"{icao}:*") if icao else None
    if rule and rule.startswith("custom:"):
        p = os.path.join(CUSTOM_DIR, rule[7:] + ".onnx")
        if os.path.exists(p):
            return p, None
    if role == "pilot":
        vid, spk = PILOT_VOICE
    else:
        acc = rule[7:] if rule and rule.startswith("accent:") and rule[7:] in ACCENTS else accent_for(icao, VOICE_PREFS.get("accents", "native"))
        vid, spk = ACCENTS[acc].get(role) or ACCENTS[acc]["tower"]
        if not _voice_ok(vid):                      # místní hlas ještě není stažený – stáhnout a zatím mluvit britsky
            _fetch_voice(vid)
            vid, spk = ACCENTS["uk"].get(role) or ACCENTS["uk"]["tower"]
    return os.path.join(PIPER_DIR, VOICE_FILES[vid][0] + ".onnx"), _speaker_id(vid, spk)


def prefetch_accent(icao):
    """Stáhne hlas přízvuku dané země dopředu (např. po načtení plánu)."""
    if not piper_state.get("installed") or not icao:
        return
    acc = accent_for(icao, VOICE_PREFS.get("accents", "native"))
    for vid, spk in ACCENTS.get(acc, {}).values():
        if not _voice_ok(vid):
            _fetch_voice(vid)


def piper_say(text, role="tower", speed=1.0, radio=True, interrupt=False, icao=None):
    if not piper_state["installed"]:
        return False
    with _piper_lock:
        if interrupt:
            _piper_q.clear()
        _piper_q.append((str(text)[:600], role, max(0.7, min(1.5, float(speed))), bool(radio), icao))
    return True


threading.Thread(target=_piper_worker, daemon=True).start()
try:
    piper_check()
except Exception:
    pass


# ---------------- ATC (fáze 1) ----------------
# Řízení celého letu podle SimBrief plánu. ATC mluví, pilot odpovídá tlačítky (1–4).
# Hlasové zprávy jdou do state["atc"]["msgs"], web je přečte nahlas.
CITY = {"LKPR": "Praha", "LKPD": "Pardubice", "LKMT": "Ostrava", "LKTB": "Brno", "LZIB": "Bratislava", "LZKZ": "Kosice", "EPKT": "Katowice",
        "EPWA": "Warszawa", "EPKK": "Krakow", "EKBI": "Billund", "EKCH": "Kastrup", "EDDN": "Nuernberg", "EDDC": "Dresden", "EDDS": "Stuttgart",
        "EDDM": "Muenchen", "EDDF": "Frankfurt", "EDDB": "Berlin", "EDDH": "Hamburg", "EDDL": "Duesseldorf", "LOWW": "Wien", "LHBP": "Budapest",
        "DAAG": "Alger", "DAOO": "Oran", "GMFO": "Oujda", "GMMN": "Casablanca", "GMMX": "Marrakech", "DTTA": "Carthage", "LEPA": "Palma",
        "LEBL": "Barcelona", "LEMD": "Madrid", "LFPG": "Paris", "LFMN": "Nice", "LIRF": "Roma", "LIMC": "Milano", "EGLL": "London", "EGKK": "Gatwick",
        "EHAM": "Schiphol", "LSZH": "Zurich", "GCTS": "Tenerife", "LGAV": "Athinai", "LTFM": "Istanbul", "OMDB": "Dubai", "LCLK": "Larnaca"}
ATC_PHASES = ["clearance", "ground", "tower_dep", "departure", "center", "approach", "tower_arr", "ground_arr", "done"]


def station_name(icao, role):
    city = CITY.get(icao)
    if not city:
        ap = next((a for a in AIRPORTS if a[0] == icao), None)
        name = (ap[1] if ap else icao) or icao
        name = re.sub(r"\b(International|Airport|Intl|Regional|Aeropuerto|Aéroport|Flughafen|Aeroporto|Letiště)\b", "", name).strip(" -/")
        city = name.split()[0] if name else icao
    return f"{city} {role}"


def parse_procs(route):
    """SID a STAR z textu trasy SimBriefu (např. LOMKI2B … KENOK2A)."""
    toks = [t for t in str(route or "").upper().split() if t]
    proc = re.compile(r"^[A-Z]{2,5}\d[A-Z]?$")
    sid = toks[0] if toks and proc.match(toks[0]) else None
    star = toks[-1] if len(toks) > 1 and proc.match(toks[-1]) else None
    return sid, star


def best_runway(icao, wind_dir):
    """Dráha nejvíc proti větru (bez větru první dostupná)."""
    best, best_d = None, None
    for apt, a, b, width in RUNWAYS:
        if apt != icao:
            continue
        for thr, end in ((a, b), (b, a)):
            f = _rwy_frame(thr[1], thr[2], thr, end)
            brg = math.degrees(math.atan2((end[2] - thr[2]) * math.cos(math.radians(thr[1])), end[1] - thr[1])) % 360
            d = 0 if wind_dir is None else abs((brg - wind_dir + 180) % 360 - 180)
            if best_d is None or d < best_d:
                best, best_d = thr[0], d
    return best


def say_alt(ft):
    ft = int(ft or 0)
    if ft >= 10000:
        return f"flight level {say_digits(str(round(ft / 100)).zfill(3))}"
    th, hu = divmod(ft, 1000)
    return f"{say_digits(str(th)) if th else ''} thousand{(' ' + say_digits(str(hu // 100)) + ' hundred') if hu else ''} feet".strip()


SIM_CMDS = []          # (událost SimConnectu, hodnota) – zpracuje smyčka simulátoru


def rwy_heading(icao, ident):
    for apt, a, b, width in RUNWAYS:
        if apt != icao:
            continue
        for thr, end in ((a, b), (b, a)):
            if str(thr[0]).upper() == str(ident or "").upper():
                return math.degrees(math.atan2((end[2] - thr[2]) * math.cos(math.radians(thr[1])), end[1] - thr[1])) % 360
    return None


def wind_on(icao, ident, wdir, wkt):
    """(čelní, boční) složka větru na dráze v kt; čelní záporná = vítr zezadu."""
    h = rwy_heading(icao, ident)
    if h is None or wdir is None or wkt is None:
        return None
    d = math.radians((wdir - h + 540) % 360 - 180)
    return round(wkt * math.cos(d)), round(abs(wkt * math.sin(d)))


class AtcSession:
    def __init__(self):
        self.enabled = False
        self.auto = False         # kopilot odpovídá sám
        self.simbrief = True      # dráhy a procedury podle SimBriefu
        self.tune = False         # ladit frekvence do COM1 standby
        self.reset()

    def reset(self):
        self.phase = "clearance"
        self.msgs = []           # [{id, t, who, station, text, say}]
        self.options = []        # [{id, label, say}]
        self.squawk = None
        self.dep_rwy = self.arr_rwy = None
        self.cleared_alt = None
        self.flags = set()
        self.awaiting = None
        self.last_sq_warn = 0
        self.opts_t = 0
        self.arr_wind = None
        self.pending_rwy = None

    # ---- pomocné ----
    def _cs(self, s):
        return say_callsign(((s.get("plan") or {}).get("callsign")) or s.get("callsign"))

    def atc(self, station, text_say, key=None):
        if key and key in self.flags:
            return
        if key:
            self.flags.add(key)
        icao = next((x for x in (getattr(self, "cur_dep", None), getattr(self, "cur_arr", None)) if x and station.startswith(station_name(x, "").strip())), None)
        if station.startswith("Control"):
            icao = None
        self.msgs.append({"id": f"a{len(self.msgs)}-{int(time.time())}", "t": time.time(), "who": "atc", "station": station, "icao": icao, "say": text_say, "text": text_say})
        del self.msgs[:-30]

    def pilot(self, text):
        self.msgs.append({"id": f"p{len(self.msgs)}-{int(time.time())}", "t": time.time(), "who": "pilot", "text": text})
        del self.msgs[:-30]

    def opts(self, *items):
        new = [{"id": i, "label": l, "say": y} for i, l, y in items]
        if [o["id"] for o in new] != [o["id"] for o in self.options]:
            self.opts_t = time.time()
        self.options = new

    def contact(self, icao, kind):
        """„Praha Tower one one eight decimal one“ – se skutečnou frekvencí ze simulátoru, když je známá."""
        role = {"delivery": "Delivery", "ground": "Ground", "tower": "Tower", "departure": "Departure", "approach": "Approach"}[kind]
        st = station_name(icao, role)
        fq = atc_freq(icao, kind)
        if not fq:
            return st
        self.next_freq = fmt_freq(fq[0])
        if self.tune:
            SIM_CMDS.append(("COM_STBY_RADIO_SET_HZ", int(fq[0])))
        return f"{st} {say_freq(fq[0])}"

    def _rwy_check(self, icao, planned, wdir, wkt, station, cs, what):
        """SimBrief dráha vs. vítr: při silném větru zezadu/zboku nabídne volbu. Vrací True, když čeká na pilota."""
        wc = wind_on(icao, planned, wdir, wkt)
        alt = best_runway(icao, wdir)
        if not wc or not alt or alt == planned:
            return False
        head, cross = wc
        if head >= -7 and cross <= 25:
            return False
        why = f"{-head} knots tailwind" if head < -7 else f"{cross} knots crosswind"
        self.pending_rwy = (what, planned, alt)
        self.atc(station, f"{cs}, be advised, runway {say_runway(planned)} has {why}, runway {say_runway(alt)} is in use. Confirm runway.")
        self.opts(("rwy_keep", f"Ponechat RWY {planned} (SimBrief)", f"We'll keep runway {planned}, {cs}."),
                  ("rwy_alt", f"Dráha {alt} (proti větru)", f"Runway {alt}, {cs}."))
        return True

    def _issue_clearance(self, s):
        plan = s.get("plan") or {}
        cs, dep, arr = self._cs(s), plan.get("dep") or "", plan.get("arr") or ""
        sid, _ = parse_procs(plan.get("route"))
        if self.dep_rwy and plan.get("dep_rwy") and self.dep_rwy != plan.get("dep_rwy"):
            sid = None                          # jiná dráha než v plánu = SID ze SimBriefu nemusí sedět
        via = f"via {sid[:-2]} {say_digits(sid[-2:])} departure" if sid else "via flight planned route"
        self.atc(station_name(dep, "Delivery"), f"{cs}, cleared to {say_airport(arr)} {via}, runway {say_runway(self.dep_rwy)}, "
                 f"{'climb via S I D' if sid else 'climb ' + say_alt(5000)}, squawk {say_digits(self.squawk)}.")
        self.awaiting = "readback_clr"
        self.opts(("rb", "Readback", f"Cleared to {arr}, runway {self.dep_rwy}, squawk {self.squawk}, {cs}."))
        if self.auto:
            SIM_CMDS.append(("XPNDR_SET", int(self.squawk, 16)))       # kopilot nastaví squawk v simulátoru

    def _issue_approach(self, s):
        plan = s.get("plan") or {}
        cs, arr = self._cs(s), plan.get("arr") or ""
        self.cleared_alt = 4000
        self.atc(station_name(arr, "Approach"), f"{cs}, radar contact, descend {say_alt(4000)}, expect I L S approach runway {say_runway(self.arr_rwy)}.")

    # ---- reakce na tlačítka ----
    def respond(self, opt_id, s):
        opt = next((o for o in self.options if o["id"] == opt_id), None)
        if not opt:
            return False
        self.pilot(opt["say"])
        self.options = []
        plan = s.get("plan") or {}
        cs, dep, arr = self._cs(s), plan.get("dep") or "", plan.get("arr") or ""
        if opt_id == "clr":
            self.squawk = self.squawk or f"{random.randint(2, 6)}{random.randint(0, 7)}{random.randint(0, 7)}{random.randint(0, 7)}"
            planned = plan.get("dep_rwy") if self.simbrief else None
            self.dep_rwy = planned or best_runway(dep, s.get("wind_dir")) or self.dep_rwy
            if planned and self._rwy_check(dep, planned, s.get("wind_dir"), s.get("wind_kt"), station_name(dep, "Delivery"), cs, "dep"):
                return True
            self._issue_clearance(s)
        elif opt_id in ("rwy_keep", "rwy_alt") and self.pending_rwy:
            what, planned, alt = self.pending_rwy
            self.pending_rwy = None
            if what == "dep":
                self.dep_rwy = planned if opt_id == "rwy_keep" else alt
                self._issue_clearance(s)
            else:
                self.arr_rwy = planned if opt_id == "rwy_keep" else alt
                self._issue_approach(s)
        elif opt_id == "rb" and self.awaiting == "readback_clr":
            self.atc(station_name(dep, "Delivery"), f"{cs}, readback correct, when ready for pushback contact {self.contact(dep, 'ground')}.")
            self.phase, self.awaiting = "ground", None
        elif opt_id == "push":
            self.atc(station_name(dep, "Ground"), f"{cs}, pushback and start up approved.")
            self.flags.add("pushed")
        elif opt_id == "taxi":
            self.atc(station_name(dep, "Ground"), f"{cs}, taxi to holding point runway {say_runway(self.dep_rwy)}.")
            self.flags.add("taxi_ok")
        elif opt_id == "ready":
            self.flags.add("ready")
            self.ready_t = time.time()
        elif opt_id in ("checkin_dep", "checkin_ctr", "checkin_app"):
            self.flags.add(opt_id)
            if opt_id == "checkin_dep":
                self.cleared_alt = min(int(plan.get("cruise") or 15000), 15000) if (plan.get("cruise") or 0) else 15000
                self.atc(station_name(dep, "Departure"), f"{cs}, radar contact, climb {say_alt(self.cleared_alt)}.")
            elif opt_id == "checkin_ctr":
                self.cleared_alt = int(plan.get("cruise") or 35000)
                self.atc("Control", f"{cs}, radar contact, climb {say_alt(self.cleared_alt)}.")
            else:
                wdir, wkt = self.arr_wind or (None, None)
                planned = plan.get("arr_rwy") if self.simbrief else None
                self.arr_rwy = planned or best_runway(arr, wdir) or self.arr_rwy
                if planned and self._rwy_check(arr, planned, wdir, wkt, station_name(arr, "Approach"), cs, "arr"):
                    return True
                self._issue_approach(s)
        elif opt_id == "wilco":
            pass
        elif opt_id == "taxi_in":
            self.atc(station_name(arr, "Ground"), f"{cs}, taxi to the stand, welcome to {say_airport(arr)}.")
            self.phase = "done"
        return True

    # ---- průběh letu ----
    def update(self, s):
        if not self.enabled:
            return
        plan = s.get("plan") or {}
        dep, arr = plan.get("dep") or "", plan.get("arr") or ""
        if not dep or not arr:
            self.options = []
            if not self.msgs:
                self.atc("ATC", "Load your SimBrief flight plan to start A T C.", key="noplan")
            return
        cs = self._cs(s)
        if (dep, arr) != (getattr(self, "cur_dep", None), getattr(self, "cur_arr", None)):
            prefetch_accent(dep)
            prefetch_accent(arr)
            fac_request(dep, diag=False)
            fac_request(arr, diag=False)
        self.cur_dep, self.cur_arr = dep, arr
        on_ground, agl, alt, gs = bool(s.get("on_ground")), s.get("agl") or 0, s.get("alt") or 0, s.get("gs") or 0
        lat, lon = s.get("lat"), s.get("lon")
        ap_arr = next((a for a in AIRPORTS if a[0] == arr), None)
        d_arr = haversine_nm(lat, lon, ap_arr[2], ap_arr[3]) if ap_arr and lat is not None else None
        ph = self.phase
        if ph == "clearance":
            if not self.options and self.awaiting is None:
                self.opts(("clr", "Request IFR clearance", f"{station_name(dep, 'Delivery')}, {cs}, request I F R clearance to {say_airport(arr)}."))
            if not on_ground:
                self.phase = "departure"
        elif ph == "ground":
            items = []
            if "pushed" not in self.flags and gs < 3:
                items.append(("push", "Request pushback", f"{station_name(dep, 'Ground')}, {cs}, request pushback and start up."))
            if "taxi_ok" not in self.flags:
                items.append(("taxi", "Request taxi", f"{station_name(dep, 'Ground')}, {cs}, request taxi."))
            self.options = [o for o in self.options if o["id"] in [i[0] for i in items]] or [{"id": i, "label": l, "say": y} for i, l, y in items]
            # kontrola squawku
            sq = str(s.get("squawk") or "")
            if self.squawk and gs > 5 and sq and sq != self.squawk and time.time() - self.last_sq_warn > 120:
                self.last_sq_warn = time.time()
                self.atc(station_name(dep, "Ground"), f"{cs}, check squawk, should be {say_digits(self.squawk)}.")
            edge = runway_near_edge(lat, lon, runways_near(lat, lon)) if lat is not None else None
            if "taxi_ok" in self.flags and edge:
                self.atc(station_name(dep, "Ground"), f"{cs}, contact {self.contact(dep, 'tower')}.", key="to_tower")
                self.phase = "tower_dep"
            if not on_ground:
                self.phase = "departure"
        elif ph == "tower_dep":
            if "ready" not in self.flags:
                self.opts(("ready", "Ready for departure", f"{station_name(dep, 'Tower')}, {cs}, holding point runway {self.dep_rwy or ''}, ready for departure."))
            else:
                self.options = []
                alerts = (s.get("tower") or {}).get("alerts") or []
                blocked = any(a.get("level") in ("warn", "danger") for a in alerts)
                if blocked:
                    self.atc(station_name(dep, "Tower"), f"{cs}, hold short runway {say_runway(self.dep_rwy)}, traffic.", key="hold_dep")
                elif "to_clr" not in self.flags:
                    self.atc(station_name(dep, "Tower"), f"{cs}, {say_wind(s)}runway {say_runway(self.dep_rwy)}, cleared for takeoff.", key="to_clr")
            if not on_ground and agl > 1500:
                self.atc(station_name(dep, "Tower"), f"{cs}, contact {self.contact(dep, 'departure')}, good day.", key="to_dep")
                self.phase = "departure"
        elif ph == "departure":
            if "checkin_dep" not in self.flags:
                self.opts(("checkin_dep", "Check in", f"{station_name(dep, 'Departure')}, {cs}, passing {say_alt(round(alt / 100) * 100)}, climbing."))
            elif alt > 13000 or (d_arr is not None and d_arr < 60):
                self.atc(station_name(dep, "Departure"), f"{cs}, contact Control, good day.", key="to_ctr")
                self.phase, self.options = "center", []
        elif ph == "center":
            if "checkin_ctr" not in self.flags:
                self.opts(("checkin_ctr", "Check in", f"Control, {cs}, {say_alt(round(alt / 100) * 100)}."))
            elif d_arr is not None:
                cruise = int(plan.get("cruise") or 35000)
                tod = cruise / 1000 * 3 + 15
                if d_arr < tod and "descend" not in self.flags:
                    _, star = parse_procs(plan.get("route"))
                    fl = 10000 if star else 7000
                    self.cleared_alt = fl
                    self.flags.add("descend")
                    def _wx(icao=arr):
                        m = fetch_metars([icao]).get(icao) or {}
                        try:
                            self.arr_wind = (None if m.get("wdir") in (None, "VRB") else float(m["wdir"]), float(m.get("wspd") or 0))
                        except Exception:
                            self.arr_wind = None
                    threading.Thread(target=_wx, daemon=True).start()
                    self.atc("Control", f"{cs}, descend {say_alt(fl)}{(', ' + star[:-2] + ' ' + say_digits(star[-2:]) + ' arrival') if star else ''}.")
                    self.opts(("wilco", "Wilco", f"Descend {fl}, {cs}."))
                if d_arr < 40 and alt < 16000:
                    self.atc("Control", f"{cs}, contact {self.contact(arr, 'approach')}.", key="to_app")
                    self.phase, self.options = "approach", []
        elif ph == "approach":
            if "checkin_app" not in self.flags:
                self.opts(("checkin_app", "Check in", f"{station_name(arr, 'Approach')}, {cs}, {say_alt(round(alt / 100) * 100)}, information received."))
            else:
                fin = on_final(lat, lon, s.get("hdg") or 0, agl, rws_big(lat, lon), max_nm=14) if lat is not None else None
                if fin:
                    self.atc(station_name(arr, "Approach"), f"{cs}, cleared I L S approach runway {say_runway(fin[1])}, contact {self.contact(arr, 'tower')}.", key="ils")
                    self.arr_rwy = fin[1]
                    self.phase, self.options = "tower_arr", []
        elif ph == "tower_arr":
            # povolení k přistání dává automatická věž; po přistání předání na Ground
            if s.get("live_landing") and on_ground and gs < 40:
                self.atc(station_name(arr, "Tower"), f"{cs}, vacate when able, contact {self.contact(arr, 'ground')}.", key="vacate")
                self.phase = "ground_arr"
        elif ph == "ground_arr":
            if not self.options:
                self.opts(("taxi_in", "Request taxi to stand", f"{station_name(arr, 'Ground')}, {cs}, runway vacated, request taxi to the stand."))
        # automatický kopilot: odpoví sám, když na to přijde čas
        if self.auto and self.options and time.time() - self.opts_t > 3:
            ids = [o["id"] for o in self.options]
            moving, stopped = gs > 3, gs < 2
            near_rwy = lat is not None and (runway_near_edge(lat, lon, runways_near(lat, lon)) or runway_occupied_by(lat, lon, runways_near(lat, lon)))
            pick = None
            for oid in ids:
                if oid in ("clr", "rb", "checkin_dep", "checkin_ctr", "checkin_app", "wilco", "rwy_alt", "taxi_in"):
                    pick = oid
                elif oid == "push" and gs > 0.5:
                    pick = oid
                elif oid == "taxi" and moving:
                    pick = oid
                elif oid == "ready" and stopped and near_rwy:
                    pick = oid
                if pick:
                    break
            if pick:
                self.respond(pick, s)
        # restart po novém plánu nebo vzletu z jiného letiště
        key = f"{dep}-{arr}-{plan.get('callsign')}"
        if getattr(self, "plan_key", None) != key:
            if getattr(self, "plan_key", None) is not None:
                self.reset()
            self.plan_key = key

    def snapshot(self):
        return {"enabled": self.enabled, "auto": self.auto, "simbrief": self.simbrief, "tune": self.tune, "next_freq": getattr(self, "next_freq", None),
                "freqs": {k: (FAC_CACHE.get(k) or {}).get("status") for k in (getattr(self, "cur_dep", None), getattr(self, "cur_arr", None)) if k}, "phase": self.phase, "msgs": self.msgs[-12:], "options": self.options, "squawk": self.squawk,
                "dep_rwy": self.dep_rwy, "arr_rwy": self.arr_rwy, "cleared_alt": self.cleared_alt}


def say_airport(icao):
    city = CITY.get(icao)
    return city or say_digits(icao)


atc = AtcSession()


def atc_loop():
    while True:
        time.sleep(2)
        try:
            with lock:
                s = dict(state)
            if s.get("connected") and not s.get("demo"):
                atc.update(s)
            with lock:
                state["atc"] = atc.snapshot()
        except Exception as e:
            with lock:
                state["atc"] = {**atc.snapshot(), "error": str(e)[:120]}


def _tag_apt(alerts, s):
    if alerts and s.get("lat") is not None:
        ap = nearest_airport(s["lat"], s["lon"], 15)
        for a in alerts:
            a.setdefault("apt", ap[0] if ap else None)
    return alerts


def tower_loop():
    """Každých pár sekund vyhodnotí provoz kolem. Polohy ostatních stahuje jen u letiště a při přiblížení."""
    others, last_fetch = [], 0.0
    while True:
        time.sleep(3)
        try:
            with lock:
                s = dict(state)
            relevant = s.get("connected") and s.get("online") and not s.get("demo") and (s.get("on_ground") or (s.get("agl") or 0) < 12000)
            if not relevant:
                with lock:
                    state["tower"] = {"alerts": [], "traffic": 0, "active": False}
                continue
            now = time.time()
            if now - last_fetch > (6 if (s.get("agl") or 0) < 3000 else 15):
                last_fetch = now
                lat, lon = s["lat"], s["lon"]
                since = datetime.fromtimestamp(now - 120, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                q = (f"/rest/v1/live?select=user_id,callsign,lat,lon,alt,gs,vs,hdg,phase,updated_at,plan_arr:plan->>arr,plan_cs:plan->>callsign"
                     f"&lat=gte.{lat - 0.6:.3f}&lat=lte.{lat + 0.6:.3f}&lon=gte.{lon - 0.9:.3f}&lon=lte.{lon + 0.9:.3f}&updated_at=gte.{since}&user_id=neq.{cloud.user_id}")
                code, res = cloud.request("GET", q)
                if code == 200 and isinstance(res, list):
                    others = res
            alerts = _tag_apt(tower_alerts(s, others), s)
            with lock:
                state["tower"] = {"alerts": alerts, "traffic": len(others), "active": True}
        except Exception as e:
            with lock:
                state["tower"] = {"alerts": [], "traffic": 0, "active": False, "error": str(e)[:120]}


# ---------------- Hodnocení přistání ----------------
def landing_score(fpm, g):
    f = abs(fpm)
    points = [(0, 10), (150, 10), (300, 7.5), (500, 4.5), (700, 1.5), (900, 0)]
    score = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 <= f <= x2:
            score = y1 + (y2 - y1) * (f - x1) / (x2 - x1)
            break
    if g > 1.3:
        score -= (g - 1.3) * 8
    return round(max(0.0, min(10.0, score)), 1)


def verdict(score):
    if score >= 9.5:
        return "Máslo!"
    if score >= 8:
        return "Výborné přistání"
    if score >= 6:
        return "Dobré přistání"
    if score >= 4:
        return "Tvrdší přistání"
    if score >= 2:
        return "Tvrdé přistání"
    return "Kontrola podvozku"


def save_landing(rec):
    try:
        data = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data.append(rec)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("Nepodařilo se uložit historii:", e)


class FlightRecorder:
    """Sleduje pojíždění, vzlet, let a přistání. Po dojetí na stání vytvoří a odešle záznam letu."""

    def __init__(self, touchdown_reader=None, upload=True):
        self.was_on_ground = None
        self.air_since = None
        self.last_air_vs = 0.0
        self.recent_g = []
        self.pending = None
        self.post = None              # dokončený let čekající na dojetí na stání
        self.last_landing_t = 0.0
        self.touchdown_reader = touchdown_reader
        self.upload = upload
        self.takeoff = None
        self.dist = 0.0
        self.last_pos = None
        self.ground_pos = None
        self.path = []                # trasa letu (pojíždění + let + pojíždění)
        self.pre_path = []            # pojíždění před vzletem
        self.last_pt_t = 0
        self.last_pt_hdg = None
        self.stopped_since = None

    # ---------- body trasy ----------
    def _publish(self, pts):
        with lock:
            state["trail"] = list(pts)
            state["trail_ver"] += 1

    def _add_point(self, t, lat, lon, alt, hdg=None, target=None):
        pts = self.path if target is None else target
        if pts and haversine_nm(pts[-1][0], pts[-1][1], lat, lon) > 40:
            pts.clear()               # skok o desítky mil = přesun letadla v simulátoru, starou trasu zahodit
        if not pts or getattr(self, "path_t0", None) is None:
            self.path_t0 = t
        pts.append([round(lat, 5), round(lon, 5), int(alt), int(t - self.path_t0), int(getattr(self, "cur_gs", 0) or 0)])
        if len(pts) > 2400:
            pts[:] = pts[:1] + pts[1:-1:2] + pts[-1:]
        self.last_pt_t = t
        self.last_pt_hdg = hdg
        self._publish(pts)

    def _maybe_point(self, t, lat, lon, alt, hdg, every, turn_deg, target=None):
        turned = self.last_pt_hdg is not None and abs((hdg - self.last_pt_hdg + 180) % 360 - 180) > turn_deg
        if t - self.last_pt_t > every or (turned and t - self.last_pt_t > 2):
            self._add_point(t, lat, lon, alt, hdg, target)

    # ---------- hlavní smyčka ----------
    def update(self, t, on_ground, vs_fpm, g, info):
        lat, lon = info.get("lat", 0), info.get("lon", 0)
        gs, hdg, alt = info.get("gs", 0), info.get("hdg", 0), info.get("alt", 0)
        agl = info.get("agl")
        self.cur_gs = gs
        # Při načítání letu hlásí MSFS chvíli „ve vzduchu“ i na stání. Za let se počítá jen opravdový pohyb:
        # rychlost nad 30 kt nebo výška nad 100 ft nad zemí. Jinak se letadlo bere jako na zemi.
        if not on_ground and (self.was_on_ground or self.was_on_ground is None) and gs < 30 and \
                ((agl is None or agl < 100) or abs(vs_fpm) < 200):
            on_ground = True       # načítání letu: letadlo „visí“ ve vzduchu bez rychlosti – není to vzlet
        if not on_ground and agl is not None:
            self.max_agl = max(getattr(self, "max_agl", None) or 0, agl)
        if self.was_on_ground is None:
            self.was_on_ground = on_ground
            if not on_ground:
                self.air_since = t
                self._start(t, lat, lon, known=False)
                log("Tracker se připojil až za letu – vzlet a pojíždění chybí.")
            elif gs > 3:
                log("Tracker se připojil až během pojíždění – trasa začíná odsud.")
            return

        if on_ground:
            if gs < 1:
                self.stopped_since = self.stopped_since or t
            else:
                self.stopped_since = None
            if gs < 5:
                self.ground_pos = (lat, lon)
            if self.post:
                # pojíždění po přistání
                if gs > 3:
                    self._maybe_point(t, lat, lon, 0, hdg, 5, 20)
                stopped = self.stopped_since and t - self.stopped_since > 30
                if stopped or time.time() - self.post["t"] > 15 * 60:
                    self.finalize(lat, lon)
            elif self.takeoff and not self.takeoff.get("landed") and not self.takeoff.get("icao") \
                    and self.stopped_since and t - self.stopped_since > 60:
                # „vzlet“ z neznámého letiště a letadlo teď stojí na zemi = falešný vzlet (načítání letu) – zahodit
                self.takeoff = None
                self.path = []
                self._publish([])
                with lock:
                    state["dep"] = None
                log("Falešný vzlet při načítání letu zahozen, čekám na skutečný vzlet.")
            elif not self.pending:
                # pojíždění před vzletem
                if gs > 3:
                    if not self.pre_path:
                        log("Zaznamenávám pojíždění…")
                    self._maybe_point(t, lat, lon, 0, hdg, 5, 20, target=self.pre_path)
                elif self.stopped_since and t - self.stopped_since > 45 * 60 and self.pre_path:
                    self.pre_path = []          # velmi dlouhé stání (přes 45 min) = začátek nového letu
                    self._publish([])
                    log("Dlouhé stání – předchozí pojíždění zahozeno.")

        if not on_ground:
            if self.was_on_ground:
                if self.pending:
                    self.pending["bounces"] += 1
                self.air_since = t
                if self.takeoff is None or self.takeoff.get("landed"):
                    if self.post:
                        self.finalize(lat, lon)  # vzlet znovu (např. touch-and-go) – předchozí let uzavřít
                    gp = self.ground_pos or (lat, lon)
                    self._start(t, gp[0], gp[1], known=True)
                    self.last_pos = (lat, lon)
            if agl is not None and agl < 4000:
                ab = getattr(self, "appr", None)
                if ab is None:
                    ab = self.appr = []
                if not ab or t - ab[-1][0] >= 1.5:
                    ab.append((t, lat, lon, agl, info.get("ias") or gs, gs, vs_fpm, hdg))
                    if len(ab) > 600:
                        del ab[:len(ab) - 600]
            elif agl is not None and agl > 6000:
                self.appr = []
            self.last_air_vs = vs_fpm
            self.recent_vs = [(tt, v) for tt, v in getattr(self, "recent_vs", []) if t - tt < 1.0] + [(t, vs_fpm)]
            self.recent_g.append((t, g))
            self._maybe_point(t, lat, lon, alt, hdg, 15, 8)
            if self.last_pos:
                step = haversine_nm(self.last_pos[0], self.last_pos[1], lat, lon)
                if step < 5:
                    self.dist += step
            self.last_pos = (lat, lon)
        elif not self.was_on_ground:
            airborne = t - (self.air_since or t)
            mx = getattr(self, "max_agl", None)
            real_flight = mx is None or mx >= 50            # nejen poskočení / načítání letu
            self.max_agl = None
            if airborne > 15 and real_flight and t - self.last_landing_t > 10:
                gmax = max([gg for tt, gg in self.recent_g if t - tt < 0.6] + [g])
                # rychlost klesání v okamžiku dosednutí: nejprudší hodnota z poslední ~0,6 s před dotykem
                pre = [v for tt, v in getattr(self, "recent_vs", []) if t - tt < 0.6] or [self.last_air_vs]
                vs_td = min(pre + [-abs(self.last_air_vs)])
                self.pending = {"t": t, "fpm": -abs(min(vs_td, 0)), "g": gmax, "info": dict(info), "bounces": 0}
                self.last_landing_t = t
                self._add_point(t, lat, lon, 0, hdg)

        if self.pending:
            self.pending["g"] = max(self.pending["g"], g)
            if t - self.pending["t"] >= 6.0:
                self._finish(info)

        self.recent_g = [(tt, gg) for tt, gg in self.recent_g if t - tt < 2]
        self.was_on_ground = on_ground

    def _start(self, t, lat, lon, known):
        ap = nearest_airport(lat, lon) if known else None
        self.takeoff = {"t": t, "iso": now_iso(), "wall": time.time(), "icao": ap[0] if ap else None,
                        "name": ap[1] if ap else None, "landed": False, "fuel": state.get("fuel_lbs")}
        self.dist = 0.0
        self.last_pos = (lat, lon)
        self.path = list(self.pre_path) if known else []
        self.pre_path = []
        if not self.path:
            self.path_t0 = None
        self.appr = []
        self.last_pt_hdg = None
        self._add_point(t, lat, lon, 0)
        with lock:
            state["dep"] = {"icao": self.takeoff["icao"], "iso": self.takeoff["iso"] if known else None}
        if known:
            log(f"🛫 Vzlet{' z ' + ap[0] if ap else ''}")
            if ap:
                to_ref, icao = self.takeoff, ap[0]
                def dep_wx():
                    got = fetch_metars([icao])
                    if icao in got:
                        to_ref["wx"] = dict(got[icao], icao=icao)
                threading.Thread(target=dep_wx, daemon=True).start()

    def state_name(self):
        if self.post:
            return "landed"
        if self.takeoff and not self.takeoff.get("landed") and not self.was_on_ground:
            return "flying"
        if self.takeoff and not self.takeoff.get("landed"):
            return "flying"          # na zemi po vzletu bez přistání (např. přerušený let)
        return "idle"

    def finalize(self, lat=None, lon=None, sync=False):
        """Uzavře dokončený let (po dojetí na stání, tlačítkem, novém vzletu nebo odpojení simulátoru) a odešle ho."""
        post, self.post = self.post, None
        with lock:
            state["live_landing"] = None
        if not post:
            return
        flight = post["flight"]
        path = list(self.path)
        if lat is not None and lon is not None and lat and lon:
            path.append([round(lat, 5), round(lon, 5), 0])
        while len(path) > 2900:
            path = path[:1] + path[1:-1:2] + path[-1:]
        flight["path"] = path
        self.path = []
        self._publish([])
        if not self.upload:
            return
        unqueue_flight(flight.get("local_id"))
        fuel_used = post["fuel_lbs"]

        upload_flight(flight, fuel_used, file_crew=True, sync=sync)

    def save_partial(self, lat=None, lon=None, sync=False):
        """Let bez přistání (zavřená aplikace nebo simulátor uprostřed letu, tlačítko Ukončit let):
        uloží, co se doletělo, jako nedokončený let."""
        to = self.takeoff
        if not to or to.get("landed"):
            return False
        to["landed"] = True
        ap = nearest_airport(lat, lon) if (lat and lon and self.was_on_ground) else None
        with lock:
            plan = dict(state["plan"]) if state.get("plan") else None
            callsign = state["callsign"]
            state["dep"] = None
        path = list(self.path)
        if lat and lon:
            path.append([round(lat, 5), round(lon, 5), int(state.get("alt") or 0)])
        while len(path) > 2900:
            path = path[:1] + path[1:-1:2] + path[-1:]
        dur = round(max(0, time.time() - (to.get("wall") or time.time())) / 60, 1)
        flight = {
            "callsign": (plan or {}).get("callsign") or callsign, "aircraft": (state.get("aircraft") or "")[:80],
            "dep_icao": to.get("icao"), "dep_name": to.get("name"),
            "arr_icao": ap[0] if ap else None, "arr_name": ap[1] if ap else None,
            "takeoff_at": to.get("iso"), "landed_at": now_iso(), "duration_min": dur,
            "distance_nm": round(self.dist, 1), "path": path, "incomplete": True,
            "plan_arr": plan and plan.get("arr"), "plan_altn": plan and plan.get("altn"),
            "flight_no": plan and plan.get("flight_no"), "route": plan and (plan.get("route") or "")[:2000],
            "sched_off": plan and plan.get("sched_off"), "sched_on": plan and plan.get("sched_on"),
            "plan_fixes": plan and plan.get("fixes"), "reg": current_reg(state),
            "ac_icao": ((plan or {}).get("aircraft") or state.get("ac_model") or None),
            "va": current_va(state, dep=to.get("icao")),
        }
        self.path = []
        self._publish([])
        self.was_on_ground = None          # další vzorek začne nový záznam
        self.pending = None
        self.air_since = None
        log("Let ukončen bez přistání – ukládám, co se doletělo.")
        if self.upload:
            upload_flight(flight, None, file_crew=False, sync=sync)
        return True

    def finish_now(self, sync=False):
        """Tlačítko Uložit let / zavření aplikace: uloží let hned."""
        lat, lon = state.get("lat"), state.get("lon")
        if self.post:
            self.finalize(lat, lon, sync=sync)
            return "saved"
        if self.save_partial(lat, lon, sync=sync):
            return "partial"
        return "nothing"

    def _finish(self, now_info=None):
        p, self.pending = self.pending, None
        self.ground_pos = None
        fpm, source = p["fpm"], "vs"
        if self.touchdown_reader:
            try:
                td = self.touchdown_reader()
                # „touchdown normal velocity“ je kolmo k povrchu dráhy – na nerovné nebo skloněné dráze (scenérie)
                # vychází mírněji. Bereme proto prudší z obou měření, jako ostatní trackery.
                if td and abs(td) > 0.1 and -abs(td) * 60.0 < fpm:
                    fpm, source = -abs(td) * 60.0, "touchdown"
            except Exception:
                pass
        fpm = round(fpm)
        g = round(p["g"], 2)
        score = landing_score(fpm, g)
        info = p["info"]
        ap = nearest_airport(info.get("lat", 0), info.get("lon", 0))
        hdg = info.get("hdg", 0)
        rwy = runway_at(info.get("lat", 0), info.get("lon", 0), hdg) if RUNWAYS else None
        head, cross = wind_components(info.get("wind_dir"), info.get("wind_kt"), hdg)
        to0 = self.takeoff or {}
        fuel_used = None
        if to0.get("fuel") is not None and state.get("fuel_lbs") is not None:
            fuel_used = max(0, round(to0["fuel"] - state["fuel_lbs"]))
        with lock:
            plan = dict(state["plan"]) if state.get("plan") else None
        rec = {
            "id": int(time.time() * 1000),
            "cas": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fpm": fpm, "g": g, "skore": score, "hodnoceni": verdict(score),
            "letadlo": info.get("aircraft", ""),
            "odlet": (self.takeoff or {}).get("icao"),
            "prilet": ap[0] if ap else None,
            "lat": round(info.get("lat", 0), 5), "lon": round(info.get("lon", 0), 5),
            "zdroj_fpm": source,
            "draha": runway_text(rwy),
            "odskoky": p["bounces"],
            "ias": round(info.get("ias", 0)), "gs": round(info.get("gs", 0)),
            "vitr": None if info.get("wind_dir") is None else [round(info["wind_dir"]), round(info["wind_kt"]), head, cross],
        }
        with lock:
            state["landing"] = rec
            state["dep"] = None
            callsign = state["callsign"]
        save_landing(rec)
        log(f"🛬 PŘISTÁNÍ{' v ' + ap[0] if ap else ''}: {fpm} fpm | {g} G | {score}/10 – {rec['hodnoceni']}")
        if rec["draha"]:
            log("   " + rec["draha"])

        to = self.takeoff or {"t": p["t"], "iso": None}
        to["landed"] = True
        flight = {
            "callsign": callsign, "aircraft": (info.get("aircraft") or "")[:80],
            "dep_icao": to.get("icao"), "dep_name": to.get("name"),
            "arr_icao": ap[0] if ap else None, "arr_name": ap[1] if ap else None,
            "takeoff_at": to.get("iso"), "landed_at": now_iso(),
            "duration_min": round(max(0, p["t"] - to["t"]) / 60, 1),
            "distance_nm": round(self.dist, 1),
            "fpm": max(-6000, min(0, fpm)), "g": min(10, g), "score": score,
            "path": None,
            "td_lat": round(info.get("lat", 0), 6), "td_lon": round(info.get("lon", 0), 6),
            "td_ias": round(info.get("ias", 0)), "td_gs": round(info.get("gs", 0)), "td_hdg": round(hdg) % 360,
            "wind_dir": None if info.get("wind_dir") is None else round(info["wind_dir"]) % 360,
            "wind_kt": None if info.get("wind_kt") is None else round(info["wind_kt"]),
            "headwind": head, "crosswind": cross, "bounces": p["bounces"],
            "rwy": rwy and rwy["rwy"], "rwy_dist_m": rwy and rwy["dist_m"], "rwy_offset_m": rwy and rwy["offset_m"],
            "fuel_used_kg": None if fuel_used is None else round(fuel_used * 0.4536),
            "plan_arr": plan and plan.get("arr"), "plan_altn": plan and plan.get("altn"),
            "flight_no": plan and plan.get("flight_no"),
            "route": plan and (plan.get("route") or "")[:2000],
            "sched_off": plan and plan.get("sched_off"), "sched_on": plan and plan.get("sched_on"),
            "plan_fixes": plan and plan.get("fixes"),
            "reg": current_reg(state),
            "ac_icao": ((plan or {}).get("aircraft") or state.get("ac_model") or None),
            "va": current_va(state, dep=to.get("icao")),
            "approach": approach_analysis(list(getattr(self, "appr", []) or []), info.get("lat", 0), info.get("lon", 0), hdg) if RUNWAYS else None,
        }
        self.appr = []
        attach_weather(flight, (self.takeoff or {}).get("wx"))
        if plan and plan.get("callsign"):
            flight["callsign"] = plan["callsign"]
        with lock:
            state["last_flight"] = {"flight": flight, "fuel_lbs": fuel_used, "crew": None}
        # let se uloží až po dojetí na stání (aby obsahoval i pojíždění); pojistka pro případ zavření aplikace:
        flight["local_id"] = rec["id"]
        if self.upload:
            queue_flight(dict(flight, hold_until=time.time() + 16 * 60))
        self.post = {"flight": flight, "fuel_lbs": fuel_used, "t": time.time()}
        with lock:
            state["live_landing"] = {k: flight.get(k) for k in ("td_lat", "td_lon", "fpm", "g", "score", "rwy", "rwy_dist_m",
                                                                 "rwy_offset_m", "td_ias", "td_gs", "wind_dir", "wind_kt",
                                                                 "headwind", "crosswind", "bounces", "arr_icao", "callsign")}
def phase_of(on_ground, vs, agl, gs):
    if on_ground:
        return "Na zemi" if gs < 40 else "Rozjezd"
    if vs > 300:
        return "Stoupání"
    if vs < -300:
        return "Klesání"
    return "Cestovní let" if agl > 3000 else "Let"


def push(on_ground, alt, agl, ias, gs, vs, hdg, g, lat, lon, aircraft):
    with lock:
        state.update({
            "connected": True, "status": "Připojeno",
            "on_ground": on_ground, "alt": round(alt), "agl": round(agl),
            "ias": round(ias), "gs": round(gs), "vs": round(vs),
            "hdg": round(hdg) % 360, "g": round(g, 2),
            "lat": lat, "lon": lon, "aircraft": aircraft,
            "phase": phase_of(on_ground, vs, agl, gs),
        })


# ---------------- Čtení z MSFS ----------------
def sim_loop():
    try:
        from SimConnect import SimConnect as _BaseSimConnect, AircraftRequests
        SimConnect = fac_make_simconnect(_BaseSimConnect)
        try:
            from SimConnect import AircraftEvents
        except Exception:
            AircraftEvents = None
    except ImportError:
        with lock:
            state["status"] = "Chybí knihovna SimConnect"
        log("Chybí knihovna SimConnect.")
        return

    rec = None
    waiting_logged = False
    while True:
        try:
            with lock:
                state["connected"] = False
                state["status"] = "Čekám na MSFS…"
            if not waiting_logged:
                log("Čekám na MSFS…")
                waiting_logged = True
            new_dll = find_new_simconnect_dll()
            sm = SimConnect(library_path=new_dll) if new_dll else SimConnect()
            fac_result["dll"] = new_dll or "starší (z knihovny)"
            aq = AircraftRequests(sm, _time=0)
            ae = AircraftEvents(sm) if AircraftEvents else None
            log("Připojeno k MSFS ✔")
            was_connected = True

            td_req = None
            try:
                from SimConnect.RequestList import Request
                td_req = Request((b"PLANE TOUCHDOWN NORMAL VELOCITY", b"Feet per second"), sm, _time=0)
            except Exception:
                td_req = None

            def read_td():
                return td_req.value if td_req is not None else None

            if rec is None:
                global RECORDER
                rec = FlightRecorder(touchdown_reader=read_td)
                rec_ref["rec"] = rec
                RECORDER = rec
            else:
                rec.touchdown_reader = read_td
            title, title_t = "", 0.0
            last_vals = {}
            wind_dir = wind_kt = None
            slow_t = 0.0
            fails = 0

            while True:
                t = time.time()
                og = aq.get("SIM_ON_GROUND")
                if og is None:
                    fails += 1
                    if fails > 100:
                        raise ConnectionError("MSFS neodpovídá")
                    time.sleep(0.1)
                    continue
                fails = 0
                on_ground = bool(og)
                def rd(key, default):
                    """Hodnota ze simulátoru; když nestihne odpovědět, použít poslední známou (ne nulu)."""
                    v = aq.get(key)
                    if v is None:
                        return last_vals.get(key, default)
                    last_vals[key] = v
                    return v
                vs = rd("VERTICAL_SPEED", 0)                 # knihovna vrací rovnou stopy za minutu
                g = rd("G_FORCE", 1.0)
                # výška jako na výškoměru (u letových hladin tlaková výška), ne skutečná nadmořská
                alt = rd("INDICATED_ALTITUDE", None)
                if alt is None:
                    alt = rd("PLANE_ALTITUDE", 0)
                agl = rd("PLANE_ALT_ABOVE_GROUND", 0)
                ias = rd("AIRSPEED_INDICATED", 0)
                gs = rd("GROUND_VELOCITY", 0)
                hdg = rd("PLANE_HEADING_DEGREES_TRUE", 0)
                if abs(hdg) <= 2 * math.pi + 0.01:
                    hdg = math.degrees(hdg)
                lat = aq.get("PLANE_LATITUDE")
                lon = aq.get("PLANE_LONGITUDE")
                if lat is None or lon is None or (abs(lat) < 1 and abs(lon) < 1) or lat == 0 or lon == 0:
                    time.sleep(0.1)       # simulátor se načítá nebo neposlal polohu – vzorek přeskočit
                    continue
                if t - title_t > 10:
                    raw = aq.get("TITLE")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "ignore")
                    title = (raw or "").strip("\x00 ")
                    title_t = t
                    try:
                        vals = []
                        for k in ("ATC_ID", "ATC_MODEL"):
                            v = aq.get(k)
                            if isinstance(v, bytes):
                                v = v.decode("utf-8", "ignore")
                            vals.append((v or "").strip("\x00 ")[:12] or None)
                        default_tail = note_sim_reg(vals[0], title)
                        with lock:
                            state["reg"], state["ac_model"] = vals
                            state["reg_default"] = default_tail
                    except Exception:
                        pass
                if t - slow_t > 2:
                    slow_t = t
                    try:
                        wind_dir = aq.get("AMBIENT_WIND_DIRECTION")
                        wind_kt = aq.get("AMBIENT_WIND_VELOCITY")
                        sq = aq.get("TRANSPONDER_CODE:1")
                        if sq is not None:
                            with lock:
                                state["squawk"] = squawk_text(sq)
                        fuel = aq.get("FUEL_TOTAL_QUANTITY_WEIGHT")
                        if fuel is not None:
                            with lock:
                                state["fuel_lbs"] = fuel
                    except Exception:
                        pass

                push(on_ground, alt, agl, ias, gs, vs, hdg, g, lat, lon, title)
                with lock:
                    state["wind_dir"], state["wind_kt"] = wind_dir, wind_kt
                rec.update(t, on_ground, vs, g, {"aircraft": title, "lat": lat, "lon": lon, "gs": gs, "ias": ias,
                                                 "alt": alt, "agl": agl, "hdg": hdg, "wind_dir": wind_dir, "wind_kt": wind_kt})
                if FAC_PENDING:
                    fac_process(sm)
                # příkazy pro simulátor (např. squawk od ATC kopilota)
                while SIM_CMDS and ae is not None:
                    name, val = SIM_CMDS.pop(0)
                    try:
                        ev = ae.find(name)
                        if ev:
                            ev(int(val))
                    except Exception as e:
                        log("Příkaz pro simulátor se nepovedl:", name, e)
                time.sleep(0.05)
        except Exception as e:
            if "Did not find" not in str(e):
                log("Spojení s MSFS přerušeno:", e)
                waiting_logged = False
            if rec is not None and rec.post:
                rec.finalize()
            elif rec is not None and rec.state_name() == "flying":
                rec.save_partial(state.get("lat"), state.get("lon"))
            time.sleep(5)


# ---------------- Zkušební let (bez MSFS) ----------------
def demo_loop():
    with lock:
        state["demo"] = True
    log("DEMO režim: simuluji let, přistání přijde zhruba každé 2 minuty. Na web se nic neposílá.")
    global RECORDER
    rec = FlightRecorder(upload=False)
    RECORDER = rec
    start = time.time()
    alt = 1200.0
    target_fpm = -random.randint(60, 480)
    last = start
    lat, lon = 50.1008, 14.26
    while True:
        now = time.time()
        dt, last = now - last, now
        t = (now - start) % 120
        if t < 1 and now - start > 5:
            target_fpm = -random.randint(60, 480)
            lat, lon = 50.1008, 14.26
        g = 1.0 + random.uniform(-0.02, 0.02)
        if t < 4:
            og, gs, vs = True, 0, 0
        elif t < 10:
            og, gs, vs = True, 15, 0
        elif t < 20:
            og, gs, vs = True, 15 + (t - 10) * 14, 0
        elif t < 50:
            og, gs, vs = False, 250, 2500
        elif t < 70:
            og, gs, vs = False, 450, 0
        elif t < 100:
            og, gs, vs = False, 300, -2000
        elif t < 107:
            og, gs, vs = False, 140, -700 + (t - 100) / 7 * (700 + target_fpm)
        elif t < 108:
            og, gs, vs = False, 138, target_fpm
        else:
            og, gs, vs = True, max(10, 138 - (t - 108) * 12), 0
            if t < 108.6:
                g = 1.0 + abs(target_fpm) / 1000 * 0.9 + random.uniform(0, 0.05)
        if og:
            alt = 1200.0
        else:
            alt = max(1200.0, alt + vs / 60 * dt * 6)
        lon -= gs * dt / 3600 / 60 * 8 / math.cos(math.radians(lat))
        agl = 0 if og else alt - 1200
        name = "Boeing 737-800 Jáchym Air 7T-JAY"
        push(og, alt, agl, gs * 0.95, gs, vs, 270, g, lat, lon, name)
        rec.update(now, og, vs, g, {"aircraft": name, "lat": lat, "lon": lon, "gs": gs, "ias": gs * 0.95, "alt": alt,
                                    "hdg": 270, "wind_dir": 250, "wind_kt": 8})
        time.sleep(0.05)


# ---------------- Webový server pro OBS ----------------
class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/overlay", "/overlay.html"):
            try:
                with open(os.path.join(RES_DIR, "overlay.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "overlay.html nenalezen".encode("utf-8"), "text/plain; charset=utf-8")
        elif path == "/data":
            with lock:
                body = json.dumps(state, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/historie":
            try:
                with open(HISTORY_FILE, "rb") as f:
                    self._send(200, f.read(), "application/json; charset=utf-8")
            except FileNotFoundError:
                self._send(200, b"[]", "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass




# ---------------- Aktualizace aplikace ----------------
def parse_ver(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def check_update():
    if not REPO:
        return
    try:
        path = "releases?per_page=15" if BETA else "releases/latest"
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{path}",
                                     headers={"User-Agent": "JachymAirTracker", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read())
        if isinstance(rel, list):          # beta: nejnovější vydání včetně testovacích
            rel = max((x for x in rel if not x.get("draft")), key=lambda x: parse_ver(x.get("tag_name")), default={})
        tag = rel.get("tag_name", "")
        if parse_ver(tag) <= parse_ver(VERSION):
            return
        asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
        with lock:
            state["update"] = {"version": tag.lstrip("v"), "url": asset and asset["browser_download_url"],
                               "page": rel.get("html_url")}
        log(f"Je k dispozici nová verze {tag.lstrip('v')}")
    except Exception:
        pass


def apply_update(info, on_progress=None):
    """Stáhne novou verzi, vymění ji za běžící .exe a spustí ji."""
    if not FROZEN or not info.get("url"):
        webbrowser.open(info.get("page") or f"https://github.com/{REPO}/releases/latest")
        return
    exe = sys.executable
    new, old = exe + ".new", exe + ".old"
    req = urllib.request.Request(info["url"], headers={"User-Agent": "JachymAirTracker"})
    with urllib.request.urlopen(req, timeout=60) as r, open(new, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and on_progress:
                on_progress(done / total)
    if total and os.path.getsize(new) != total:
        os.remove(new)
        raise IOError("Stažený soubor je neúplný, zkus to znovu.")

    def retry(fn, *args):
        # antivirus si čerstvě stažený soubor na pár sekund zamyká – zkoušej to znovu
        last = None
        for _ in range(40):
            try:
                return fn(*args)
            except OSError as e:
                last = e
                time.sleep(0.5)
        raise last

    if os.path.exists(old):
        try:
            retry(os.remove, old)
        except OSError:
            old = exe + f".old{int(time.time())}"
    retry(os.rename, exe, old)
    try:
        retry(os.rename, new, exe)
    except OSError:
        os.rename(old, exe)          # vrátit původní verzi, ať aplikace dál funguje
        raise
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # nová verze nesmí převzít dočasné soubory té staré
    subprocess.Popen([exe, "--po-aktualizaci"], close_fds=True, env=env)
    os._exit(0)


def cleanup_old_exe():
    if FROZEN:
        import glob
        for f in glob.glob(sys.executable + ".old*") + [sys.executable + ".new"]:
            try:
                os.remove(f)
            except OSError:
                pass


def start_background(demo=False):
    def web_login():
        if not cloud.has_saved():
            return
        with lock:
            state["web_status"] = "Přihlašuji…"
        while True:
            res = cloud.try_saved()
            if res is True:
                return
            if res is False:
                with lock:
                    state["web_status"] = "Přihlášení vypršelo, přihlas se znovu"
                return
            with lock:
                state["web_status"] = "Web nedostupný, zkouším znovu…"
            time.sleep(15)

    load_settings()
    threading.Thread(target=lambda: (load_airports(), load_runways()), daemon=True).start()
    threading.Thread(target=web_login, daemon=True).start()
    threading.Thread(target=demo_loop if demo else sim_loop, daemon=True).start()
    threading.Thread(target=upload_loop, daemon=True).start()
    threading.Thread(target=tower_loop, daemon=True).start()
    threading.Thread(target=atc_loop, daemon=True).start()
    threading.Thread(target=discord_loop, daemon=True).start()
    threading.Thread(target=check_update, daemon=True).start()

    def bids_loop():
        while True:
            try:
                crew.refresh_bids()
            except Exception:
                pass
            time.sleep(300)
    threading.Thread(target=bids_loop, daemon=True).start()
