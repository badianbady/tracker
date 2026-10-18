# -*- coding: utf-8 -*-
"""
Jáchym Air Tracker – aplikace pro Windows.
Okno zobrazuje web JAI Trackeru (mapa, profily, žebříčky) a k tomu panel trackeru.
Kdyby se web uvnitř okna nepodařilo zobrazit, spustí se jednoduché záložní okno.
"""

import os
import subprocess
import sys
import threading
import time
import traceback
import tkinter  # noqa: F401  (načteno hned, aby se při sestavení vždy zabalilo i záložní okno)

import tracker_core as core

ERROR_FILE = os.path.join(core.DATA_DIR, "chyby.txt")


def write_error(title, exc_text):
    try:
        with open(ERROR_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n===== {title} (verze {core.VERSION}) =====\n{exc_text}\n")
    except Exception:
        pass


def message_box(text, title="Jáchym Air Tracker"):
    """Okno se zprávou bez závislosti na tkinteru."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        pass


class Api:
    """Funkce, které může web uvnitř aplikace volat přes window.pywebview.api."""

    def get_state(self):
        with core.lock:
            s = {k: v for k, v in core.state.items() if k not in ("trail", "log", "plan", "last_flight", "crew")}
            s["log"] = [list(x) for x in core.state["log"][-20:]]
            s["crew"] = dict(core.state["crew"])
            plan = core.state.get("plan")
            s["plan"] = {k: v for k, v in plan.items() if k != "fixes"} if plan else None
            lf = core.state.get("last_flight")
            s["last_flight"] = {"dep": lf["flight"].get("dep_icao"), "arr": lf["flight"].get("arr_icao"),
                                "crew": lf.get("crew")} if lf else None
        s["version"] = core.VERSION
        s["va_now"] = core.current_va(core.state)
        s["reg_now"] = core.current_reg(core.state)
        s["obs_url"] = f"http://localhost:{core.PORT}"
        return s

    def login(self, email, password):
        ok, msg = core.cloud.login(email, password)
        return {"ok": ok, "msg": msg}

    def logout(self):
        core.cloud.logout()
        return True

    # ---- SimBrief ----
    def load_simbrief(self, user=None):
        user = (user or core.state.get("simbrief_user") or "").strip()
        plan, err = core.fetch_simbrief(user)
        if err:
            return {"ok": False, "msg": err}
        core.set_plan(plan)
        if user != core.state.get("simbrief_user") and core.state["online"]:
            if core.cloud.save_profile({"simbrief_user": user}):
                with core.lock:
                    core.state["simbrief_user"] = user
        return {"ok": True}

    def clear_plan(self):
        core.set_plan(None)
        return True

    # ---- Crew center ----
    def crew_link(self, key):
        ok, msg = core.crew.link(key)
        return {"ok": ok, "msg": msg}

    def crew_unlink(self):
        core.crew.unlink()
        return True

    def crew_set_auto(self, auto):
        core.crew.set_auto(auto)
        return True

    def crew_bid(self, flight_id, aircraft=None):
        if not core.crew.linked:
            return {"ok": False, "msg": "Nejdřív propoj crew center v záložce Tracker."}
        ok, msg = core.crew.add_bid(flight_id, aircraft)
        return {"ok": ok, "msg": msg}

    def crew_unbid(self, flight_id):
        ok, msg = core.crew.remove_bid(flight_id)
        return {"ok": ok, "msg": msg}

    def crew_refresh(self):
        core.crew.refresh_bids()
        return True

    def crew_file_last(self):
        lf = core.state.get("last_flight")
        if not lf:
            return {"ok": False, "msg": "Zatím není žádný let k odeslání."}
        if not core.crew.linked:
            return {"ok": False, "msg": "Nejdřív propoj crew center."}
        status = core.crew.file_flight(lf["flight"], lf.get("fuel_lbs"))
        return {"ok": "✔" in status, "msg": status}

    def set_reg(self, reg):
        core.set_reg(reg)
        return True

    def set_va(self, code):
        core.set_va(code)
        return True

    def open_url(self, url):
        import webbrowser
        if str(url).startswith(("https://", "http://localhost", "http://127.0.0.1")):
            webbrowser.open(url)
            return True
        return False

    def copy_text(self, text):
        try:
            subprocess.run("clip", input=str(text).encode("utf-16-le"), check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        except Exception:
            return False

    def do_update(self):
        info = core.state.get("update")
        if not info:
            return False

        with core.lock:
            core.state["update_error"] = None

        def prog(p):
            with core.lock:
                core.state["update_progress"] = round(p * 100)

        def work():
            try:
                core.apply_update(info, prog)
            except Exception as e:
                core.log("Aktualizace se nepovedla:", e)
                write_error("Aktualizace", traceback.format_exc())
                with core.lock:
                    core.state["update_progress"] = None
                    core.state["update_error"] = f"Aktualizace se nepovedla ({e}). Původní verze zůstala, zkus to znovu."
        threading.Thread(target=work, daemon=True).start()
        return True


def run_webview():
    import webview
    url = core.web_url()
    if not url:
        raise RuntimeError("Adresa webu není nastavená")
    webview.create_window(
        "Jáchym Air Tracker", f"{url}?app=1&v={int(time.time())}", js_api=Api(),
        width=1360, height=860, min_size=(960, 620), background_color="#F3F6F4",
    )
    storage = os.path.join(core.DATA_DIR, "webview")
    os.makedirs(storage, exist_ok=True)
    webview.start(private_mode=False, storage_path=storage, icon=os.path.join(core.RES_DIR, "icon.ico"))


def main():
    server = None
    # po aktualizaci chvíli počkej, než se stará verze úplně vypne a uvolní port
    for _ in range(30 if "--po-aktualizaci" in sys.argv else 3):
        try:
            server = core.QuietServer(("127.0.0.1", core.PORT), core.Handler)
            break
        except OSError:
            time.sleep(0.5)
    core.cleanup_old_exe()
    if server is None:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showwarning("Jáchym Air Tracker", "Tracker už běží v jiném okně.")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    core.start_background(demo="--demo" in sys.argv)

    if "--jednoduche" not in sys.argv:
        try:
            run_webview()
            return
        except Exception as e:
            write_error("Web uvnitř aplikace", traceback.format_exc())
            core.log("Web uvnitř aplikace nejde zobrazit, používám jednoduché okno:", repr(e))
    import app_tk
    app_tk.run_tk()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        write_error("Pád aplikace", tb)
        message_box("Aplikace narazila na chybu a musí se zavřít.\n\n"
                    f"Podrobnosti jsou uložené v:\n{ERROR_FILE}\n\n{tb[-600:]}")
