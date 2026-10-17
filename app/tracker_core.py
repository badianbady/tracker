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
RUNWAYS_FILE = os.path.join(DATA_DIR, "drahy_v1.json")
CREW_FILE = os.path.join(DATA_DIR, "crew.json")
RUNWAYS_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/runways.csv"
CREW_URL = "https://crew.jachymair.com"


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
    "plan": None,
    "reg": None,
    "ac_model": None,
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


def runway_text(info):
    if not info:
        return None
    off = info["offset_m"]
    side = "na ose" if abs(off) < 0.5 else f"{abs(off):.1f} m {'vpravo' if off > 0 else 'vlevo'} od osy".replace(".", ",")
    dist = info["dist_m"]
    where = f"{dist} m za prahem" if dist >= 0 else f"{-dist} m před prahem"
    return f"RWY {info['rwy']} · {where} · {side}"


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
            cs = self.profile.get("callsign") or CALLSIGN
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
        body["reg"] = s.get("reg") or ((s.get("plan") or {}).get("reg"))
        body["takeoff_at"] = (s.get("dep") or {}).get("iso")
        if with_trail:
            body["trail"] = s["trail"]
        if with_plan:
            body["plan"] = s["plan"]
        if s.get("plan") and s["plan"].get("callsign"):
            body["callsign"] = s["plan"]["callsign"]
        return self.request("POST", "/rest/v1/live?on_conflict=user_id", body, "resolution=merge-duplicates,return=minimal")

    def send_flight(self, flight):
        code, res = self.request("POST", "/rest/v1/flights", flight, "return=minimal")
        return code in (200, 201)


cloud = Cloud()


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


def set_plan(plan):
    with lock:
        state["plan"] = plan
        state["plan_ver"] += 1
    if plan:
        log(f"Letový plán: {plan['dep']} → {plan['arr']}" + (f" (alt. {plan['altn']})" if plan.get("altn") else ""))


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
    sent_plan_ver = -1
    while True:
        time.sleep(5)
        if not state["online"] or state["demo"]:
            continue
        with lock:
            s = dict(state)
        if s["connected"]:
            try:
                with_trail = s["trail_ver"] != sent_trail_ver and (time.time() - last_trail_t > 20 or not s["trail"])
                with_plan = s["plan_ver"] != sent_plan_ver
                code, res = cloud.send_live(s, with_trail, with_plan)
                if with_trail and code in (200, 201, 204):
                    sent_trail_ver, last_trail_t = s["trail_ver"], time.time()
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
                if self.pending:
                    self.pending["bounces"] += 1
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
                self.pending = {"t": t, "fpm": -abs(self.last_air_vs), "g": gmax, "info": dict(info), "bounces": 0}
                self.last_landing_t = t

        if self.pending:
            self.pending["g"] = max(self.pending["g"], g)
            if t - self.pending["t"] >= 6.0:
                self._finish(info)

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
        self.takeoff = {"t": t, "iso": now_iso(), "icao": ap[0] if ap else None, "name": ap[1] if ap else None,
                        "landed": False, "fuel": state.get("fuel_lbs")}
        self.dist = 0.0
        self.last_pos = (lat, lon)
        self.path = []
        self.last_pt_hdg = None
        self._add_point(t, lat, lon, 0)
        with lock:
            state["dep"] = {"icao": self.takeoff["icao"], "iso": self.takeoff["iso"] if known else None}
        if known:
            log(f"🛫 Vzlet{' z ' + ap[0] if ap else ''}")

    def _finish(self, now_info=None):
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
            state["trail"] = []
            state["trail_ver"] += 1
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
            "path": self.path + [[round(info.get("lat", 0), 4), round(info.get("lon", 0), 4), 0]],
            "td_lat": round(info.get("lat", 0), 6), "td_lon": round(info.get("lon", 0), 6),
            "td_ias": round(info.get("ias", 0)), "td_gs": round(info.get("gs", 0)), "td_hdg": round(hdg) % 360,
            "wind_dir": None if info.get("wind_dir") is None else round(info["wind_dir"]) % 360,
            "wind_kt": None if info.get("wind_kt") is None else round(info["wind_kt"]),
            "headwind": head, "crosswind": cross, "bounces": p["bounces"],
            "rwy": rwy and rwy["rwy"], "rwy_dist_m": rwy and rwy["dist_m"], "rwy_offset_m": rwy and rwy["offset_m"],
            "fuel_used_kg": None if fuel_used is None else round(fuel_used * 0.4536),
            "plan_arr": plan and plan.get("arr"), "plan_altn": plan and plan.get("altn"),
            "route": plan and (plan.get("route") or "")[:2000],
            "sched_off": plan and plan.get("sched_off"), "sched_on": plan and plan.get("sched_on"),
            "plan_fixes": plan and plan.get("fixes"),
            "reg": ((plan or {}).get("reg") or state.get("reg") or None),
            "ac_icao": ((plan or {}).get("aircraft") or state.get("ac_model") or None),
        }
        if plan and plan.get("callsign"):
            flight["callsign"] = plan["callsign"]
        with lock:
            state["last_flight"] = {"flight": flight, "fuel_lbs": fuel_used, "crew": None}
        if self.upload and state["online"]:
            def send():
                if crew.linked and crew.auto:
                    flight["crew_status"] = crew.file_flight(flight, fuel_used)
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
                    try:
                        vals = []
                        for k in ("ATC_ID", "ATC_MODEL"):
                            v = aq.get(k)
                            if isinstance(v, bytes):
                                v = v.decode("utf-8", "ignore")
                            vals.append((v or "").strip("\x00 ")[:12] or None)
                        with lock:
                            state["reg"], state["ac_model"] = vals
                    except Exception:
                        pass
                if t - slow_t > 2:
                    slow_t = t
                    try:
                        wind_dir = aq.get("AMBIENT_WIND_DIRECTION")
                        wind_kt = aq.get("AMBIENT_WIND_VELOCITY")
                        fuel = aq.get("FUEL_TOTAL_QUANTITY_WEIGHT")
                        if fuel is not None:
                            with lock:
                                state["fuel_lbs"] = fuel
                    except Exception:
                        pass

                push(on_ground, alt, agl, ias, gs, vs, hdg, g, lat, lon, title)
                rec.update(t, on_ground, vs, g, {"aircraft": title, "lat": lat, "lon": lon, "gs": gs, "ias": ias,
                                                 "alt": alt, "hdg": hdg, "wind_dir": wind_dir, "wind_kt": wind_kt})
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
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # nová verze nesmí převzít dočasné soubory té staré
    subprocess.Popen([exe, "--po-aktualizaci"], close_fds=True, env=env)
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

    threading.Thread(target=lambda: (load_airports(), load_runways()), daemon=True).start()
    threading.Thread(target=web_login, daemon=True).start()
    threading.Thread(target=demo_loop if demo else sim_loop, daemon=True).start()
    threading.Thread(target=upload_loop, daemon=True).start()
    threading.Thread(target=check_update, daemon=True).start()

    def bids_loop():
        while True:
            try:
                crew.refresh_bids()
            except Exception:
                pass
            time.sleep(300)
    threading.Thread(target=bids_loop, daemon=True).start()
