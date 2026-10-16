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

import tracker_core as core


class Api:
    """Funkce, které může web uvnitř aplikace volat přes window.pywebview.api."""

    def get_state(self):
        with core.lock:
            s = {k: v for k, v in core.state.items() if k not in ("trail", "log")}
            s["log"] = [list(x) for x in core.state["log"][-20:]]
        s["version"] = core.VERSION
        s["obs_url"] = f"http://localhost:{core.PORT}"
        return s

    def login(self, email, password):
        ok, msg = core.cloud.login(email, password)
        return {"ok": ok, "msg": msg}

    def logout(self):
        core.cloud.logout()
        return True

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

        def prog(p):
            with core.lock:
                core.state["update_progress"] = round(p * 100)

        def work():
            try:
                core.apply_update(info, prog)
            except Exception as e:
                core.log("Aktualizace se nepovedla:", e)
                with core.lock:
                    core.state["update_progress"] = None
        threading.Thread(target=work, daemon=True).start()
        return True


def run_webview():
    import webview
    url = core.web_url()
    if not url:
        raise RuntimeError("Adresa webu není nastavená")
    webview.create_window(
        "Jáchym Air Tracker", url + "?app=1", js_api=Api(),
        width=1360, height=860, min_size=(960, 620), background_color="#F3F6F4",
    )
    storage = os.path.join(core.DATA_DIR, "webview")
    os.makedirs(storage, exist_ok=True)
    webview.start(private_mode=False, storage_path=storage, icon=os.path.join(core.RES_DIR, "icon.ico"))


def main():
    core.cleanup_old_exe()
    try:
        server = core.QuietServer(("127.0.0.1", core.PORT), core.Handler)
    except OSError:
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
            core.log("Web uvnitř aplikace nejde zobrazit, používám jednoduché okno:", e)
    import app_tk
    app_tk.run_tk()


if __name__ == "__main__":
    main()
