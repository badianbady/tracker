# -*- coding: utf-8 -*-
"""Jáchym Air Tracker – jednoduché okno (záloha, když nejde zobrazit web uvnitř aplikace)."""

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import messagebox

import tracker_core as core

GREEN = "#1E9B36"
GREEN_DEEP = "#116B26"
GREEN_SOFT = "#E3F5E7"
BG = "#F3F6F4"
SURFACE = "#FFFFFF"
TEXT = "#102217"
MUTED = "#5C6E63"
LINE = "#DCE5DF"
RED = "#B91C1C"
ORANGE = "#C2410C"

FONT = "Segoe UI"


def score_color(s):
    if s is None:
        return MUTED
    return GREEN if s >= 8 else "#8A9A18" if s >= 6 else ORANGE if s >= 3 else RED


# ---------------- Okno ----------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("Jáchym Air Tracker")
        root.configure(bg=BG)
        root.geometry("460x700")
        root.minsize(420, 620)
        try:
            root.iconbitmap(os.path.join(core.RES_DIR, "icon.ico"))
        except Exception:
            pass
        self.shown_landing = None

        self._header()
        self.update_bar = tk.Frame(root, bg="#FFF4D6")
        self.update_lbl = tk.Label(self.update_bar, bg="#FFF4D6", fg=TEXT, font=(FONT, 10, "bold"), anchor="w")
        self.update_lbl.pack(side="left", padx=14, pady=8)
        self.update_btn = self._button(self.update_bar, "Aktualizovat", self.do_update, small=True)
        self.update_btn.pack(side="right", padx=10, pady=6)

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.login_frame = self._build_login()
        self.main_frame = self._build_main()

        self.show_login() if not core.cloud.has_saved() else self.show_main()
        self.tick()

    # ---------- stavební prvky ----------
    def _header(self):
        c = tk.Canvas(self.root, height=72, bg=GREEN, highlightthickness=0)
        c.pack(fill="x")
        # logo radar
        c.create_oval(18, 14, 62, 58, outline="white", width=3)
        c.create_oval(27, 23, 53, 49, outline="#BFE8C9", width=2)
        c.create_oval(34, 30, 48, 44, outline="#8FD3A1", width=2)
        c.create_line(40, 36, 55, 21, fill="white", width=3, capstyle="round")
        c.create_oval(37, 33, 43, 39, fill="white", outline="")
        c.create_text(76, 28, text="Jáchym Air Tracker", fill="white", font=(FONT, 16, "bold"), anchor="w")
        c.create_text(76, 50, text=f"verze {core.VERSION}", fill="#CFEFD6", font=(FONT, 9), anchor="w")

    def _button(self, parent, text, cmd, ghost=False, small=False):
        b = tk.Button(parent, text=text, command=cmd, cursor="hand2", relief="flat", bd=0,
                      font=(FONT, 9 if small else 10, "bold"), padx=12 if small else 14, pady=4 if small else 8,
                      bg=SURFACE if ghost else GREEN, fg=GREEN_DEEP if ghost else "white",
                      activebackground=GREEN_SOFT if ghost else GREEN_DEEP,
                      activeforeground=GREEN_DEEP if ghost else "white",
                      highlightthickness=1 if ghost else 0, highlightbackground=GREEN)
        return b

    def _card(self, parent, **pack):
        f = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE)
        f.pack(fill="x", padx=16, **pack)
        return f

    def _entry(self, parent, show=None):
        e = tk.Entry(parent, font=(FONT, 11), relief="flat", bg=BG, fg=TEXT, insertbackground=TEXT,
                     highlightthickness=1, highlightbackground=LINE, highlightcolor=GREEN, show=show)
        e.pack(fill="x", padx=18, ipady=6)
        return e

    # ---------- přihlášení ----------
    def _build_login(self):
        f = tk.Frame(self.body, bg=BG)
        tk.Label(f, text="Přihlášení", bg=BG, fg=TEXT, font=(FONT, 18, "bold")).pack(anchor="w", padx=18, pady=(26, 0))
        tk.Label(f, text="Použij stejný e-mail a heslo jako na webu.", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", padx=18, pady=(0, 14))
        card = self._card(f)
        tk.Label(card, text="E-mail", bg=SURFACE, fg=TEXT, font=(FONT, 10, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
        self.email = self._entry(card)
        tk.Label(card, text="Heslo", bg=SURFACE, fg=TEXT, font=(FONT, 10, "bold")).pack(anchor="w", padx=18, pady=(12, 4))
        self.password = self._entry(card, show="•")
        self.password.bind("<Return>", lambda e: self.do_login())
        self.login_err = tk.Label(card, text="", bg=SURFACE, fg=RED, font=(FONT, 10), wraplength=360, justify="left")
        self.login_err.pack(anchor="w", padx=18, pady=(8, 0))
        self.login_btn = self._button(card, "Přihlásit se", self.do_login)
        self.login_btn.pack(fill="x", padx=18, pady=(8, 18))
        if core.web_url():
            link = tk.Label(f, text="Nemáš účet? Zaregistruj se na webu", bg=BG, fg=GREEN, cursor="hand2",
                            font=(FONT, 10, "underline"))
            link.pack(pady=16)
            link.bind("<Button-1>", lambda e: webbrowser.open(core.web_url() + "#/ucet"))
        tk.Label(f, text="Bez přihlášení funguje jen OBS overlay.", bg=BG, fg=MUTED, font=(FONT, 9)).pack()
        skip = tk.Label(f, text="Pokračovat bez přihlášení", bg=BG, fg=MUTED, cursor="hand2", font=(FONT, 9, "underline"))
        skip.pack(pady=4)
        skip.bind("<Button-1>", lambda e: self.show_main())
        return f

    def do_login(self):
        email, pw = self.email.get().strip(), self.password.get()
        if not email or not pw:
            self.login_err.config(text="Vyplň e-mail i heslo.")
            return
        self.login_btn.config(state="disabled", text="Přihlašuji…")
        self.login_err.config(text="")

        def work():
            ok, msg = core.cloud.login(email, pw)
            self.root.after(0, lambda: self._login_done(ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _login_done(self, ok, msg):
        self.login_btn.config(state="normal", text="Přihlásit se")
        if ok:
            self.password.delete(0, "end")
            self.show_main()
        else:
            self.login_err.config(text=msg)

    # ---------- hlavní obrazovka ----------
    def _build_main(self):
        f = tk.Frame(self.body, bg=BG)

        st = self._card(f, pady=(16, 0))
        self.web_lbl = self._status_row(st, "Web")
        self.sim_lbl = self._status_row(st, "MSFS")
        self.phase_lbl = self._status_row(st, "Fáze letu", last=True)

        nums = self._card(f, pady=(12, 0))
        grid = tk.Frame(nums, bg=SURFACE)
        grid.pack(fill="x", padx=10, pady=10)
        self.num = {}
        for i, (key, label) in enumerate([("alt", "Výška ft"), ("ias", "IAS kt"), ("gs", "GS kt"), ("vs", "V/S fpm")]):
            cell = tk.Frame(grid, bg=SURFACE)
            cell.grid(row=0, column=i, sticky="nsew", padx=4)
            grid.columnconfigure(i, weight=1)
            v = tk.Label(cell, text="–", bg=SURFACE, fg=TEXT, font=(FONT, 17, "bold"))
            v.pack()
            tk.Label(cell, text=label, bg=SURFACE, fg=MUTED, font=(FONT, 9)).pack()
            self.num[key] = v

        tk.Label(f, text="Poslední přistání", bg=BG, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        lc = self._card(f)
        row = tk.Frame(lc, bg=SURFACE)
        row.pack(fill="x")
        self.score_box = tk.Frame(row, bg=MUTED, width=110, height=96)
        self.score_box.pack(side="left", fill="y")
        self.score_box.pack_propagate(False)
        self.score_lbl = tk.Label(self.score_box, text="–", bg=MUTED, fg="white", font=(FONT, 28, "bold"))
        self.score_lbl.pack(expand=True)
        info = tk.Frame(row, bg=SURFACE)
        info.pack(side="left", fill="both", expand=True, padx=14, pady=10)
        self.verdict_lbl = tk.Label(info, text="Zatím žádné přistání", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold"), anchor="w")
        self.verdict_lbl.pack(fill="x")
        self.fpm_lbl = tk.Label(info, text="Přistání se tu ukáže automaticky.", bg=SURFACE, fg=MUTED, font=(FONT, 10), anchor="w")
        self.fpm_lbl.pack(fill="x", pady=(4, 0))
        self.where_lbl = tk.Label(info, text="", bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w")
        self.where_lbl.pack(fill="x")

        btns = tk.Frame(f, bg=BG)
        btns.pack(fill="x", padx=16, pady=(16, 0))
        self._button(btns, "Otevřít mapu", self.open_map).pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.obs_btn = self._button(btns, "Odkaz pro OBS", self.copy_obs, ghost=True)
        self.obs_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        tk.Label(f, text="Události", bg=BG, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        self.log_box = tk.Text(f, height=6, bg=SURFACE, fg=TEXT, font=(FONT, 9), relief="flat",
                               highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=16)

        foot = tk.Frame(f, bg=BG)
        foot.pack(fill="x", padx=16, pady=10)
        self.account_link = tk.Label(foot, text="", bg=BG, fg=MUTED, cursor="hand2", font=(FONT, 9, "underline"))
        self.account_link.pack(side="right")
        self.account_link.bind("<Button-1>", lambda e: self.account_action())
        return f

    def _status_row(self, parent, name, last=False):
        r = tk.Frame(parent, bg=SURFACE)
        r.pack(fill="x", padx=16, pady=(12, 12 if last else 0))
        tk.Label(r, text=name, bg=SURFACE, fg=MUTED, font=(FONT, 10), width=10, anchor="w").pack(side="left")
        v = tk.Label(r, text="–", bg=SURFACE, fg=TEXT, font=(FONT, 10, "bold"), anchor="w")
        v.pack(side="left", fill="x", expand=True)
        return v

    # ---------- akce ----------
    def show_login(self):
        self.main_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)
        self.email.focus_set()

    def show_main(self):
        self.login_frame.pack_forget()
        self.main_frame.pack(fill="both", expand=True)

    def open_map(self):
        url = core.web_url()
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("Mapa", "Adresa webu není v této verzi nastavená.")

    def copy_obs(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(f"http://localhost:{core.PORT}")
        self.obs_btn.config(text="Zkopírováno ✔")
        self.root.after(2000, lambda: self.obs_btn.config(text="Odkaz pro OBS"))

    def account_action(self):
        if core.state["online"]:
            if messagebox.askyesno("Odhlásit", "Opravdu se chceš odhlásit?"):
                core.cloud.logout()
                self.show_login()
        else:
            self.show_login()

    def do_update(self):
        info = core.state.get("update")
        if not info:
            return
        self.update_btn.config(state="disabled", text="Stahuji…")

        def prog(p):
            self.root.after(0, lambda: self.update_btn.config(text=f"Stahuji {int(p * 100)} %"))

        def work():
            try:
                core.apply_update(info, prog)
            except Exception as e:
                self.root.after(0, lambda: (self.update_btn.config(state="normal", text="Aktualizovat"),
                                            messagebox.showerror("Aktualizace", f"Aktualizace se nepovedla:\n{e}")))
        threading.Thread(target=work, daemon=True).start()

    # ---------- obnova obrazovky ----------
    def tick(self):
        with core.lock:
            s = dict(core.state)
            logs = list(core.state["log"])
        nf = lambda n: f"{n:,}".replace(",", " ")

        if s["online"]:
            self.web_lbl.config(text=f"✔ {s.get('username') or 'Přihlášen'}  ·  {s['callsign']}", fg=GREEN_DEEP)
            self.account_link.config(text="Odhlásit se")
        else:
            self.web_lbl.config(text=s.get("web_status") or "Nepřihlášen", fg=ORANGE)
            self.account_link.config(text="Přihlásit se")
        if s["connected"]:
            self.sim_lbl.config(text="✔ Připojeno", fg=GREEN_DEEP)
            self.phase_lbl.config(text=s["phase"] + (f"  ·  z {s['dep']['icao']}" if s.get("dep") and s["dep"].get("icao") else ""))
            self.num["alt"].config(text=nf(s["alt"]))
            self.num["ias"].config(text=str(s["ias"]))
            self.num["gs"].config(text=str(s["gs"]))
            self.num["vs"].config(text=("+" if s["vs"] > 0 else "") + nf(s["vs"]))
        else:
            self.sim_lbl.config(text=s["status"], fg=ORANGE)
            self.phase_lbl.config(text="–")
            for v in self.num.values():
                v.config(text="–")

        l = s.get("landing")
        if l and l["id"] != self.shown_landing:
            self.shown_landing = l["id"]
            col = score_color(l["skore"])
            self.score_box.config(bg=col)
            self.score_lbl.config(bg=col, text=f"{l['skore']:g}".replace(".", ","))
            self.verdict_lbl.config(text=l["hodnoceni"])
            self.fpm_lbl.config(text=f"{nf(l['fpm'])} fpm  ·  {str(l['g']).replace('.', ',')} G", fg=TEXT)
            route = f"{l.get('odlet') or '????'} → {l.get('prilet') or '????'}"
            self.where_lbl.config(text=f"{route}  ·  {l['cas'][11:16]}")

        upd = s.get("update")
        if upd and not self.update_bar.winfo_ismapped():
            self.update_lbl.config(text=f"Nová verze {upd['version']} je připravená")
            self.update_bar.pack(fill="x", before=self.body)

        if logs and (len(logs), logs[-1]) != (getattr(self, "_log_len", 0), getattr(self, "_log_last", None)):
            self._log_len, self._log_last = len(logs), logs[-1]
            self.log_box.config(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("end", "\n".join(f"{t}  {m}" for t, m in reversed(logs)))
            self.log_box.config(state="disabled")

        self.root.after(250, self.tick)


def run_tk():
    root = tk.Tk()
    App(root)

    def on_close():
        core.on_exit()           # rozpracovaný let uložit, ne zahodit
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
