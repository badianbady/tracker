# -*- coding: utf-8 -*-
"""
Jáchym Air Tracker – aplikace pro Windows
Čte data z MSFS (SimConnect), měří přistání, zobrazuje OBS overlay
a posílá polohu a dokončené lety na web JAI Trackeru.
"""

import csv
import re
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
except ImportError:
    VERSION = "0.4.0-dev"
    REPO = ""

FROZEN = getattr(sys, "frozen", False)
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "JachymAirTracker")
os.makedirs(DATA_DIR, exist_ok=True)
HISTORY_FILE = os.path.join(DATA_DIR, "historie_pristani.json")
ACCOUNT_FILE = os.path.join(DATA_DIR, "ucet.json")
QUEUE_FILE = os.path.join(DATA_DIR, "neodeslane_lety.json")
AIRPORTS_FILE = os.path.join(DATA_DIR, "letiste_v2.json")


def web_url():
    if not REPO or "/" not in REPO:
        return ""
    owner, name = REPO.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{name}/"

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
        code, res = self.request("GET", f"/rest/v1/profiles?id=eq.{self.user_id}&select=username,callsign")
        if code == 200 and res:
            self.profile = res[0]
            cs = self.profile.get("callsign") or CALLSIGN
            with lock:
                state["callsign"] = cs
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

    def send_live(self, s, with_trail=False):
        body = {
            "user_id": self.user_id, "callsign": s["callsign"], "aircraft": s["aircraft"][:80],
            "lat": s["lat"], "lon": s["lon"], "alt": s["alt"], "gs": s["gs"], "hdg": s["hdg"],
            "vs": s["vs"], "phase": s["phase"], "dep_icao": (s.get("dep") or {}).get("icao"),
        }
        if with_trail:
            body["trail"] = s["trail"]
        return self.request("POST", "/rest/v1/live?on_conflict=user_id", body, "resolution=merge-duplicates,return=minimal")

    def send_flight(self, flight):
        code, res = self.request("POST", "/rest/v1/flights", flight, "return=minimal")
        return code in (200, 201)


cloud = Cloud()


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
    left = [fl for fl in data if not cloud.send_flight(fl)]
    if left:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(left, f, ensure_ascii=False, indent=2)
    else:
        os.remove(QUEUE_FILE)
        log(f"Odesláno {len(data)} dříve neodeslaných letů ✔")


def upload_loop():
    last_flush = 0
    sent_trail_ver, last_trail_t = -1, 0
    while True:
        time.sleep(5)
        if not state["online"] or state["demo"]:
            continue
        with lock:
            s = dict(state)
        if s["connected"]:
            try:
                with_trail = s["trail_ver"] != sent_trail_ver and (time.time() - last_trail_t > 20 or not s["trail"])
                code, res = cloud.send_live(s, with_trail)
                if with_trail and code in (200, 201, 204):
                    sent_trail_ver, last_trail_t = s["trail_ver"], time.time()
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
    """Sleduje vzlet, uletěnou vzdálenost a přistání; po přistání vytvoří záznam letu."""

    def __init__(self, touchdown_reader=None, upload=True):
        self.was_on_ground = None
        self.air_since = None
        self.last_air_vs = 0.0
        self.recent_g = []
        self.pending = None
        self.last_landing_t = 0.0
        self.touchdown_reader = touchdown_reader
        self.upload = upload
        self.takeoff = None
        self.dist = 0.0
        self.last_pos = None
        self.ground_pos = None
        self.path = []
        self.last_pt_t = 0
        self.last_pt_hdg = None

    def update(self, t, on_ground, vs_fpm, g, info):
        lat, lon = info.get("lat", 0), info.get("lon", 0)
        if self.was_on_ground is None:
            self.was_on_ground = on_ground
            if not on_ground:
                self.air_since = t
                self._start(t, lat, lon, known=False)
            return

        if on_ground and info.get("gs", 99) < 5:
            self.ground_pos = (lat, lon)
        if not on_ground:
            if self.was_on_ground:
                self.air_since = t
                if self.takeoff is None or self.takeoff.get("landed"):
                    gp = self.ground_pos or (lat, lon)
                    self._start(t, gp[0], gp[1], known=True)
                    self.last_pos = (lat, lon)
            self.last_air_vs = vs_fpm
            self.recent_g.append((t, g))
            hdg = info.get("hdg", 0)
            turned = self.last_pt_hdg is not None and abs((hdg - self.last_pt_hdg + 180) % 360 - 180) > 8
            if t - self.last_pt_t > 15 or (turned and t - self.last_pt_t > 3):
                self._add_point(t, lat, lon, info.get("alt", 0), hdg)
            if self.last_pos:
                step = haversine_nm(self.last_pos[0], self.last_pos[1], lat, lon)
                if step < 5:
                    self.dist += step
            self.last_pos = (lat, lon)
        elif not self.was_on_ground:
            airborne = t - (self.air_since or t)
            if airborne > 15 and t - self.last_landing_t > 10:
                gmax = max([gg for tt, gg in self.recent_g if t - tt < 0.6] + [g])
                self.pending = {"t": t, "fpm": -abs(self.last_air_vs), "g": gmax, "info": dict(info)}
                self.last_landing_t = t

        if self.pending:
            self.pending["g"] = max(self.pending["g"], g)
            if t - self.pending["t"] >= 2.0:
                self._finish()

        self.recent_g = [(tt, gg) for tt, gg in self.recent_g if t - tt < 2]
        self.was_on_ground = on_ground

    def _add_point(self, t, lat, lon, alt, hdg=None):
        self.path.append([round(lat, 4), round(lon, 4), int(alt)])
        if len(self.path) > 2400:
            self.path = self.path[::2]
        self.last_pt_t = t
        self.last_pt_hdg = hdg
        with lock:
            state["trail"] = list(self.path)
            state["trail_ver"] += 1

    def _start(self, t, lat, lon, known):
        ap = nearest_airport(lat, lon) if known else None
        self.takeoff = {"t": t, "iso": now_iso(), "icao": ap[0] if ap else None, "name": ap[1] if ap else None, "landed": False}
        self.dist = 0.0
        self.last_pos = (lat, lon)
        self.path = []
        self.last_pt_hdg = None
        self._add_point(t, lat, lon, 0)
        with lock:
            state["dep"] = {"icao": self.takeoff["icao"]}
        if known:
            log(f"🛫 Vzlet{' z ' + ap[0] if ap else ''}")

    def _finish(self):
        p, self.pending = self.pending, None
        self.ground_pos = None
        fpm, source = p["fpm"], "vs"
        if self.touchdown_reader:
            try:
                td = self.touchdown_reader()
                if td and abs(td) > 0.1:
                    fpm, source = -abs(td) * 60.0, "touchdown"
            except Exception:
                pass
        fpm = round(fpm)
        g = round(p["g"], 2)
        score = landing_score(fpm, g)
        info = p["info"]
        ap = nearest_airport(info.get("lat", 0), info.get("lon", 0))
        rec = {
            "id": int(time.time() * 1000),
            "cas": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fpm": fpm, "g": g, "skore": score, "hodnoceni": verdict(score),
            "letadlo": info.get("aircraft", ""),
            "odlet": (self.takeoff or {}).get("icao"),
            "prilet": ap[0] if ap else None,
            "lat": round(info.get("lat", 0), 5), "lon": round(info.get("lon", 0), 5),
            "zdroj_fpm": source,
        }
        with lock:
            state["landing"] = rec
            state["dep"] = None
            state["trail"] = []
            state["trail_ver"] += 1
            callsign = state["callsign"]
        save_landing(rec)
        log(f"🛬 PŘISTÁNÍ{' v ' + ap[0] if ap else ''}: {fpm} fpm | {g} G | {score}/10 – {rec['hodnoceni']}")

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
            "path": self.path + [[round(info.get("lat", 0), 4), round(info.get("lon", 0), 4), 0]],
        }
        if self.upload and state["online"]:
            def send():
                if cloud.send_flight(flight):
                    log("   Let uložen na web ✔")
                else:
                    queue_flight(flight)
                    log("   Web nedostupný, let se odešle později.")
            threading.Thread(target=send, daemon=True).start()
        elif self.upload:
            queue_flight(flight)


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
        from SimConnect import SimConnect, AircraftRequests
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
            sm = SimConnect()
            aq = AircraftRequests(sm, _time=0)
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
                rec = FlightRecorder(touchdown_reader=read_td)
            else:
                rec.touchdown_reader = read_td
            title, title_t = "", 0.0
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
                vs = (aq.get("VERTICAL_SPEED") or 0) * 60.0
                g = aq.get("G_FORCE") or 1.0
                alt = aq.get("PLANE_ALTITUDE") or 0
                agl = aq.get("PLANE_ALT_ABOVE_GROUND") or 0
                ias = aq.get("AIRSPEED_INDICATED") or 0
                gs = aq.get("GROUND_VELOCITY") or 0
                hdg = aq.get("PLANE_HEADING_DEGREES_TRUE") or 0
                if abs(hdg) <= 2 * math.pi + 0.01:
                    hdg = math.degrees(hdg)
                lat = aq.get("PLANE_LATITUDE") or 0
                lon = aq.get("PLANE_LONGITUDE") or 0
                if t - title_t > 10:
                    raw = aq.get("TITLE")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "ignore")
                    title = (raw or "").strip("\x00 ")
                    title_t = t

                push(on_ground, alt, agl, ias, gs, vs, hdg, g, lat, lon, title)
                rec.update(t, on_ground, vs, g, {"aircraft": title, "lat": lat, "lon": lon, "gs": gs, "alt": alt, "hdg": hdg})
                time.sleep(0.05)
        except Exception as e:
            if "Did not find" not in str(e):
                log("Spojení s MSFS přerušeno:", e)
                waiting_logged = False
            time.sleep(5)


# ---------------- Zkušební let (bez MSFS) ----------------
def demo_loop():
    with lock:
        state["demo"] = True
    log("DEMO režim: simuluji let, přistání přijde zhruba každé 2 minuty. Na web se nic neposílá.")
    rec = FlightRecorder(upload=False)
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
        rec.update(now, og, vs, g, {"aircraft": name, "lat": lat, "lon": lon, "gs": gs, "alt": alt, "hdg": 270})
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
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": "JachymAirTracker", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read())
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
    if os.path.exists(old):
        os.remove(old)
    os.rename(exe, old)
    os.rename(new, exe)
    subprocess.Popen([exe], close_fds=True)
    os._exit(0)


def cleanup_old_exe():
    if FROZEN:
        try:
            os.remove(sys.executable + ".old")
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

    threading.Thread(target=load_airports, daemon=True).start()
    threading.Thread(target=web_login, daemon=True).start()
    threading.Thread(target=demo_loop if demo else sim_loop, daemon=True).start()
    threading.Thread(target=upload_loop, daemon=True).start()
    threading.Thread(target=check_update, daemon=True).start()
