# -*- coding: utf-8 -*-
"""
UI Tkinter — Central Bank Monitoring (TE Calendar + Central Banks)
- Onglets: Dashboard, News, (placeholders: Data, Historique, Démo, Logs)
- Dashboard: état en direct des heartbeats par source (Fed, BoE, ECB, BoJ, BoC, RBA, RBNZ, SNB, TECal)
- News: lance/arrête watcher_te_calendar.py (TE) et watcher_multi.py (CB), lit [HB]/[DATA]/[ANALYSIS]
- Historique JSONL (history_news.jsonl)
- Boutons: Start/Stop/Restart (individuels et All), Nettoyer (Logs/News/Historique), Exporter
"""

import os
import sys
import re
import csv
import json
import shlex
import time
import queue
import threading
import subprocess
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

APP_TITLE = "Central Bank Monitoring — UI"
HISTORY_FILE = "history_news.jsonl"


def utcnow_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def join_cmd_pretty(cmd):
    try:
        return shlex.join(cmd)
    except Exception:
        return " ".join(cmd)


def short(val):
    if val is None:
        return ""
    return str(val)


# =============================== Dashboard ===============================

class DashboardTab(ttk.Frame):
    """
    Tableau d'état live pour toutes les sources (banques centrales + TECal).
    Met à jour via update_from_hb() à chaque ligne [HB].
    """
    SOURCES_DEFAULT = [
        "Fed", "BoE", "ECB", "BoJ", "BoC", "RBA", "RBNZ", "SNB", "TECal"
    ]

    def __init__(self, master):
        super().__init__(master)

        # state: name -> dict(status, latency_ms, last_hb_iso, mode, notes)
        self.state = {s: {"status": "idle", "latency_ms": None, "last_hb": None, "mode": "", "notes": ""} for s in self.SOURCES_DEFAULT}

        # UI
        self._build_table()
        self._refresh_table_full()

    def _build_table(self):
        wrapper = ttk.Frame(self)
        wrapper.pack(fill="both", expand=True, padx=8, pady=8)

        cols = ("source", "status", "latency", "last_hb", "mode", "notes")
        self.tree = ttk.Treeview(wrapper, columns=cols, show="headings", height=14)

        self.tree.heading("source", text="Source")
        self.tree.heading("status", text="Status")
        self.tree.heading("latency", text="Latency")
        self.tree.heading("last_hb", text="Last HB (UTC)")
        self.tree.heading("mode", text="Mode")
        self.tree.heading("notes", text="Notes")

        widths = [110, 90, 90, 180, 120, 560]
        for c, w in zip(cols, widths):
            self.tree.column(c, width=w, anchor="w")

        vsb = ttk.Scrollbar(wrapper, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # tags couleur
        self.tree.tag_configure("ok", background="#eaffea")      # vert pâle
        self.tree.tag_configure("warn", background="#fff2cc")    # jaune pâle
        self.tree.tag_configure("stop", background="#ffdcdc")    # rouge pâle
        self.tree.tag_configure("idle", background="#f2f2f2")    # gris

    def _refresh_table_full(self):
        self.tree.delete(*self.tree.get_children())
        for name, st in self.state.items():
            self._insert_or_update_row(name, st)

    def _insert_or_update_row(self, name, st):
        tag = st.get("status") or "idle"
        lat = st["latency_ms"]
        lat_txt = f"{lat} ms" if isinstance(lat, int) else ""
        last_hb = st["last_hb"] or ""
        mode = st.get("mode", "")
        notes = st.get("notes", "")
        vals = (name, tag, lat_txt, last_hb, mode, notes)

        iid = f"row::{name}"
        if self.tree.exists(iid):
            self.tree.item(iid, values=vals, tags=(tag,))
        else:
            self.tree.insert("", "end", iid=iid, values=vals, tags=(tag,))

    def _set_state(self, name, **kwargs):
        if name not in self.state:
            self.state[name] = {"status": "idle", "latency_ms": None, "last_hb": None, "mode": "", "notes": ""}
        self.state[name].update(kwargs)
        self._insert_or_update_row(name, self.state[name])

    HB_RE = re.compile(
        r"^\[HB\]\s+(?P<class>alive|warn|stop|info)\s+source=(?P<source>\S+)(?:\s+status=(?P<status>\S+))?(?:\s+latency=(?P<lat>\d+)ms)?(?P<rest>.*)$",
        re.I,
    )

    def update_from_hb_line(self, line: str):
        """
        Parse une ligne [HB] brute venant de TE ou CB et met à jour le tableau.
        Accepte par ex:
          [HB] alive source=Fed status=ok latency=25ms
          [HB] alive source=TECal status=ok latency=420ms via_api
          [HB] info source=TECal api_items=3 parsed=3
          [HB] warn source=TECal api_error err=...
          [HB] stop source=TECal reason=KeyboardInterrupt
        """
        m = self.HB_RE.match(line.strip())
        if not m:
            return

        cls = (m.group("class") or "").lower()
        src = m.group("source") or ""
        status = (m.group("status") or "").lower() or ("ok" if cls == "alive" else cls)
        lat = m.group("lat")
        rest = m.group("rest") or ""

        # Mode: détecte via_api / via_ff dans rest
        mode = ""
        if "via_api" in rest:
            mode = "API"
        elif "via_ff" in rest:
            mode = "FF"

        # Notes courtes
        notes = rest.strip()
        if len(notes) > 140:
            notes = notes[:140] + " …"

        now_iso = utcnow_iso()

        # Normalise quelques sources TE qui se baladent
        if src.lower() in ("te", "tecal"):
            src = "TECal"

        # Clamp sur nos sources; si inconnu on l'ajoute
        if cls == "alive":
            self._set_state(src, status="ok", latency_ms=int(lat) if lat else None, last_hb=now_iso, mode=mode, notes=notes)
        elif cls == "info":
            # Ne change pas le status si on a déjà ok / warn ; juste enrichit notes/mode/last_hb
            cur = self.state.get(src, {})
            cur_status = cur.get("status", "idle")
            self._set_state(src, status=cur_status, latency_ms=cur.get("latency_ms"), last_hb=now_iso, mode=mode or cur.get("mode",""), notes=notes)
        elif cls == "warn":
            self._set_state(src, status="warn", latency_ms=int(lat) if lat else None, last_hb=now_iso, mode=mode, notes=notes)
        elif cls == "stop":
            self._set_state(src, status="stop", latency_ms=None, last_hb=now_iso, mode=mode, notes=notes)


# ================================== News ==================================

class NewsTab(ttk.Frame):
    """
    Un onglet unique qui gère 2 watchers en parallèle:
      - TE (TradingEconomics calendar)
      - CB (watcher_multi: banques centrales)
    Alimente le Dashboard via dashboard.update_from_hb_line(line)
    """
    def __init__(self, master, dashboard: DashboardTab):
        super().__init__(master)
        self.dashboard = dashboard

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.te_script = os.path.join(base_dir, "watcher_te_calendar.py")
        self.cb_script = os.path.join(base_dir, "watcher_multi.py")

        # --- State TE
        self.proc_te = None
        self.reader_te = None
        self.state_te = {"expect": None, "last_data": None}

        # --- State CB
        self.proc_cb = None
        self.reader_cb = None
        self.state_cb = {"expect": None, "last_data": None}

        # Queue commune (on y pousse {origin, line})
        self.q = queue.Queue()

        # Map event_id -> tree iid
        self.rows_by_event = {}
        self._log_lines = 0

        # --- UI
        self._build_options_toolbar()    # options TE + boutons + CB poll
        self._build_table()              # tableau unifié
        self._build_logs_and_status()    # logs et status bar

        # Poller de la queue
        self.after(50, self._process_queue)

    # ------------------------------------------------------------------ UI
    def _build_options_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x", padx=6, pady=6)

        # ---- Options TE
        self.var_ff       = tk.BooleanVar(value=True)
        self.var_api      = tk.BooleanVar(value=False)
        self.var_api_strict = tk.BooleanVar(value=False)
        self.var_fx8      = tk.BooleanVar(value=True)
        self.var_emit     = tk.BooleanVar(value=True)
        self.var_test     = tk.BooleanVar(value=False)
        self.var_demo     = tk.BooleanVar(value=False)
        self.var_verbose  = tk.BooleanVar(value=True)
        self.var_force    = tk.BooleanVar(value=False)

        ttk.Label(bar, text="TE options:").grid(row=0, column=0, sticky="w", padx=(0,8))
        ttk.Checkbutton(bar, text="FF", variable=self.var_ff).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(bar, text="API", variable=self.var_api).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(bar, text="Strict/pays", variable=self.var_api_strict).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(bar, text="FX8", variable=self.var_fx8).grid(row=0, column=4, sticky="w")
        ttk.Checkbutton(bar, text="Emit initial", variable=self.var_emit).grid(row=0, column=5, sticky="w")
        ttk.Checkbutton(bar, text="Test", variable=self.var_test).grid(row=0, column=6, sticky="w")
        ttk.Checkbutton(bar, text="Demo FX8", variable=self.var_demo).grid(row=0, column=7, sticky="w")
        ttk.Checkbutton(bar, text="Verbose", variable=self.var_verbose).grid(row=0, column=8, sticky="w")
        ttk.Checkbutton(bar, text="Force emit", variable=self.var_force).grid(row=0, column=9, sticky="w")

        ttk.Label(bar, text="Days:").grid(row=0, column=10, sticky="e")
        self.var_days = tk.StringVar(value="1")
        ttk.Entry(bar, width=4, textvariable=self.var_days).grid(row=0, column=11, sticky="w", padx=(2,8))

        ttk.Label(bar, text="Importance:").grid(row=0, column=12, sticky="e")
        self.var_importance = tk.StringVar(value="1,2,3")
        ttk.Entry(bar, width=8, textvariable=self.var_importance).grid(row=0, column=13, sticky="w", padx=(2,8))

        ttk.Label(bar, text="Countries:").grid(row=0, column=14, sticky="e")
        self.var_countries = tk.StringVar(value="United States,Euro Area,United Kingdom,Japan,Canada,Australia,New Zealand,Switzerland")
        ttk.Entry(bar, width=44, textvariable=self.var_countries).grid(row=0, column=15, sticky="w", padx=(2,8))

        ttk.Label(bar, text="TE key:").grid(row=0, column=16, sticky="e")
        self.var_te_key = tk.StringVar(value=os.getenv("TE_API_KEY", ""))
        ttk.Entry(bar, width=22, textvariable=self.var_te_key, show="*").grid(row=0, column=17, sticky="w", padx=(2,0))

        # ---- Options CB (poll 25ms)
        ttk.Label(bar, text="CB poll (s):").grid(row=1, column=0, sticky="e", pady=(6,0))
        self.var_cb_poll = tk.StringVar(value="0.025")  # 25ms comme avant
        ttk.Entry(bar, width=8, textvariable=self.var_cb_poll).grid(row=1, column=1, sticky="w", padx=(2,8), pady=(6,0))

        # ---- Boutons contrôle
        btns = ttk.Frame(self)
        btns.pack(side="top", fill="x", padx=6, pady=(0,6))

        # TE
        self.btn_start_te = ttk.Button(btns, text="▶ Démarrer TE", command=self.start_te)
        self.btn_stop_te  = ttk.Button(btns, text="■ Arrêter TE", command=self.stop_te, state="disabled")

        # CB
        self.btn_start_cb = ttk.Button(btns, text="▶ Démarrer CB", command=self.start_cb)
        self.btn_stop_cb  = ttk.Button(btns, text="■ Arrêter CB", command=self.stop_cb, state="disabled")

        # All
        self.btn_start_all = ttk.Button(btns, text="▶▶ Start All", command=self.start_all)
        self.btn_stop_all  = ttk.Button(btns, text="■ ■ Stop All", command=self.stop_all)
        self.btn_restart_all = ttk.Button(btns, text="↻ Restart All", command=self.restart_all)

        # Maintenance
        self.btn_clean_logs = ttk.Button(btns, text="🧹 Logs", command=self.clean_logs)
        self.btn_clean_news = ttk.Button(btns, text="🧹 News", command=self.clean_news)
        self.btn_clean_hist = ttk.Button(btns, text="🧹 Historique", command=self.clean_history)
        self.btn_export = ttk.Button(btns, text="⬇ Exporter…", command=self.export_table)

        for i, b in enumerate([
            self.btn_start_te, self.btn_stop_te,
            self.btn_start_cb, self.btn_stop_cb,
            self.btn_start_all, self.btn_stop_all, self.btn_restart_all,
            self.btn_clean_logs, self.btn_clean_news, self.btn_clean_hist, self.btn_export
        ]):
            b.grid(row=0, column=i, padx=4, pady=2, sticky="w")

    def _build_table(self):
        frame = ttk.Frame(self)
        frame.pack(side="top", fill="both", expand=True, padx=6, pady=(0,6))

        cols = ("time","source","country","currency","event","actual","consensus","previous","importance")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=16)

        self.tree.heading("time", text="Time")
        self.tree.heading("source", text="Source")
        self.tree.heading("country", text="Country")
        self.tree.heading("currency", text="Currency")
        self.tree.heading("event", text="Event")
        self.tree.heading("actual", text="Actual")
        self.tree.heading("consensus", text="Consensus")
        self.tree.heading("previous", text="Previous")
        self.tree.heading("importance", text="Imp.")

        widths = [160,90,150,80,420,80,90,90,55]
        for c, w in zip(cols, widths):
            self.tree.column(c, width=w, anchor="w")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Tags de surbrillance
        self.tree.tag_configure("impact_high", background="#ffdcdc")
        self.tree.tag_configure("impact_med",  background="#fff2cc")

    def _build_logs_and_status(self):
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="both", padx=6, pady=(0,6))

        ttk.Label(bottom, text="Logs").pack(anchor="w")
        self.txt_logs = ScrolledText(bottom, height=8, wrap="none")
        self.txt_logs.pack(fill="both", expand=True)
        self.txt_logs.configure(state="disabled")

        self.status = ttk.Label(self, text="Prêt.", anchor="w")
        self.status.pack(side="bottom", fill="x", padx=6, pady=(0,6))

    # ----------------------------------------------------------- Commands
    def _build_cmd_te(self):
        py = sys.executable
        cmd = [py, self.te_script]
        if self.var_ff.get():         cmd.append("--ff")
        if self.var_api.get():        cmd.append("--api")
        if self.var_api_strict.get(): cmd.append("--api-strict-countries")
        if self.var_fx8.get():        cmd.append("--fx8")
        if self.var_emit.get():       cmd.append("--emit-initial")
        if self.var_test.get():       cmd.append("--test")
        if self.var_demo.get():       cmd.append("--demo-fx8")
        if self.var_verbose.get():    cmd.append("--verbose")
        if self.var_force.get():      cmd.append("--force-emit")

        days = self.var_days.get().strip()
        if days: cmd += ["--days", days]
        imp = self.var_importance.get().strip()
        if imp:  cmd += ["--importance", imp]
        countries = self.var_countries.get().strip()
        if countries: cmd += ["--countries", countries]
        te = self.var_te_key.get().strip()
        if te: cmd += ["--te-key", te]
        return cmd

    def _build_cmd_cb(self):
        """
        watcher_multi.py attend des flags (pas d'argument positionnel).
        On passe donc --poll 0.025 + les banques et paramètres comme avant.
        """
        poll = self.var_cb_poll.get().strip() or "0.025"
        return [
            sys.executable, self.cb_script,
            "--poll", poll,
            "--banks", "boe,fed,ecb,boj,boc,rba,rbnz,snb",
            "--k", "5",
            "--cooldown", "60",
    ]


    # ---------------------------------------------------------- Start/Stop TE
    def start_te(self):
        if self.proc_te:
            return
        cmd = self._build_cmd_te()
        self._log_line(f"[UI] Lancement TE: {join_cmd_pretty(cmd)}")

        try:
            self.proc_te = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(self.te_script)
            )
        except Exception as e:
            self._log_line(f"[UI] erreur lancement TE: {e}")
            return

        self.reader_te = threading.Thread(target=self._reader_loop, args=("TE",), daemon=True)
        self.reader_te.start()
        self.btn_start_te.configure(state="disabled")
        self.btn_stop_te.configure(state="normal")
        self.status.configure(text="TE démarré.")
        self.after(1000, self._check_te)

    def stop_te(self):
        if not self.proc_te:
            return
        try:
            self.proc_te.terminate()
        except Exception:
            pass
        self.proc_te = None
        self.btn_start_te.configure(state="normal")
        self.btn_stop_te.configure(state="disabled")
        self.status.configure(text="TE arrêté.")
        self._log_line("[UI] TE arrêté.")

    def _check_te(self):
        if self.proc_te:
            rc = self.proc_te.poll()
            if rc is not None:
                self._log_line(f"[UI] TE exit rc={rc}")
                try:
                    leftover = self.proc_te.stdout.read() or ""
                    if leftover:
                        for ln in leftover.splitlines():
                            self._log_line(ln)
                except Exception:
                    pass
                self.proc_te = None
                self.btn_start_te.configure(state="normal")
                self.btn_stop_te.configure(state="disabled")
                self.status.configure(text=f"TE terminé (rc={rc}).")
                return
            self.after(1000, self._check_te)

    # ---------------------------------------------------------- Start/Stop CB
    def start_cb(self):
        if self.proc_cb:
            return
        cmd = self._build_cmd_cb()
        self._log_line(f"[UI] Lancement CB: {join_cmd_pretty(cmd)}")

        try:
            self.proc_cb = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(self.cb_script)
            )
        except Exception as e:
            self._log_line(f"[UI] erreur lancement CB: {e}")
            return

        self.reader_cb = threading.Thread(target=self._reader_loop, args=("CB",), daemon=True)
        self.reader_cb.start()
        self.btn_start_cb.configure(state="disabled")
        self.btn_stop_cb.configure(state="normal")
        self.status.configure(text="CB démarré.")
        self.after(1000, self._check_cb)

    def stop_cb(self):
        if not self.proc_cb:
            return
        try:
            self.proc_cb.terminate()
        except Exception:
            pass
        self.proc_cb = None
        self.btn_start_cb.configure(state="normal")
        self.btn_stop_cb.configure(state="disabled")
        self.status.configure(text="CB arrêté.")
        self._log_line("[UI] CB arrêté.")

    def _check_cb(self):
        if self.proc_cb:
            rc = self.proc_cb.poll()
            if rc is not None:
                self._log_line(f"[UI] CB exit rc={rc}")
                try:
                    leftover = self.proc_cb.stdout.read() or ""
                    if leftover:
                        for ln in leftover.splitlines():
                            self._log_line(ln)
                except Exception:
                    pass
                self.proc_cb = None
                self.btn_start_cb.configure(state="normal")
                self.btn_stop_cb.configure(state="disabled")
                self.status.configure(text=f"CB terminé (rc={rc}).")
                return
            self.after(1000, self._check_cb)

    # ------------------------------------------------------ Start/Stop All
    def start_all(self):
        self.start_te()
        # petite latence pour éviter collision stdout init
        self.after(150, self.start_cb)

    def stop_all(self):
        self.stop_te()
        self.stop_cb()

    def restart_all(self):
        self.stop_all()
        self.after(250, self.start_all)

    # ----------------------------------------------------- Reader + Queue
    def _reader_loop(self, origin: str):
        """origin = 'TE' ou 'CB' ; on pousse {origin,line} dans la queue."""
        proc = self.proc_te if origin == "TE" else self.proc_cb
        try:
            while proc is not None:
                line = proc.stdout.readline()
                if not line:
                    rc = proc.poll()
                    if rc is not None:
                        self.q.put({"origin": origin, "line": f"[HB] stop source={('TECal' if origin=='TE' else 'CB')} reason=proc_exit rc={rc}"})
                        break
                    time.sleep(0.05)
                    continue
                self.q.put({"origin": origin, "line": line.rstrip("\r\n")})
        except Exception as e:
            self.q.put({"origin": origin, "line": f"[HB] warn source={( 'TECal' if origin=='TE' else 'CB')} err=reader_error:{e}"})
        finally:
            self.q.put({"origin": origin, "line": f"[HB] info source={( 'TECal' if origin=='TE' else 'CB')} reader_done"})

    def _process_queue(self):
        try:
            while True:
                it = self.q.get_nowait()
                self._handle_line(it["origin"], it["line"])
        except queue.Empty:
            pass
        self.after(50, self._process_queue)

    # --------------------------------------------------------- Parser mixte
    def _handle_line(self, origin: str, line: str):
        # Log brut
        self._log_line(f"[{origin}] {line}")

        # --- Normalisation pour le Dashboard : accepter "[CB] [HB] ..." ou "[TE] [HB] ..."
        hb_line = line
        if hb_line.startswith("[CB] "):
            hb_line = hb_line[5:]
        elif hb_line.startswith("[TE] "):
            hb_line = hb_line[5:]

        if hb_line.startswith("[HB]"):
            # alimente le dashboard (parse HB ok/warn/stop/info + latence + mode)
            self.dashboard.update_from_hb_line(hb_line)
            return


        # Dashboard: capture tous les [HB] bruts
        if line.startswith("[HB]"):
            # alimente le dashboard (il parse tout seul)
            self.dashboard.update_from_hb_line(line)
            # continue (mais ne modifie pas l'état DATA/ANALYSIS)
            return

        # State machine séparée par origin
        state = self.state_te if origin == "TE" else self.state_cb

        if line == "[DATA]":
            state["expect"] = "DATA"
            return
        if line == "[ANALYSIS]":
            state["expect"] = "ANALYSIS"
            return

        if state["expect"] == "DATA":
            try:
                obj = json.loads(line)
                state["last_data"] = obj
                self._append_history({"type": "data", "origin": origin, "payload": obj})
                self._upsert_table_from_data(obj, origin)
            except Exception as e:
                self._log_line(f"[UI] DATA parse error ({origin}): {e}")
            finally:
                state["expect"] = None
            return

        if state["expect"] == "ANALYSIS":
            try:
                obj = json.loads(line)
                self._append_history({"type": "analysis", "origin": origin, "payload": obj})
                self._apply_analysis(obj)
            except Exception as e:
                self._log_line(f"[UI] ANALYSIS parse error ({origin}): {e}")
            finally:
                state["expect"] = None
            return

    # ----------------------------------------------------------- Table/Hist
    def _upsert_table_from_data(self, ev: dict, origin: str):
        eid = ev.get("event_id") or f"{origin}:{utcnow_iso()}"
        row_id = self.rows_by_event.get(eid)

        values = (
            ev.get("calendar_time") or ev.get("published_at") or "",
            ev.get("source") or origin,
            ev.get("country") or "",
            ev.get("currency") or "",
            ev.get("event") or ev.get("category") or "",
            short(ev.get("actual")),
            short(ev.get("consensus")),
            short(ev.get("previous")),
            short(ev.get("importance")),
        )

        if row_id:
            self.tree.item(row_id, values=values)
        else:
            row_id = self.tree.insert("", "end", values=values)
            self.rows_by_event[eid] = row_id

    def _apply_analysis(self, an: dict):
        eid = an.get("event_id")
        if not eid:
            return
        row_id = self.rows_by_event.get(eid)
        if not row_id:
            return

        impact = (an.get("impact") or "").lower()
        labels = an.get("labels") or []
        tags = []
        lset = [str(l).lower() for l in labels]
        if impact == "high" or "important" in lset:
            tags.append("impact_high")
        elif impact == "medium":
            tags.append("impact_med")
        self.tree.item(row_id, tags=tuple(tags))

    def _append_history(self, record: dict):
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log_line(f"[UI] history_write_error: {e}")

    # ------------------------------------------------------------- Actions
    def clean_logs(self):
        self.txt_logs.configure(state="normal")
        self.txt_logs.delete("1.0", "end")
        self.txt_logs.configure(state="disabled")
        self._log_lines = 0

    def clean_news(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.rows_by_event.clear()

    def clean_history(self):
        try:
            if os.path.exists(HISTORY_FILE):
                os.remove(HISTORY_FILE)
            self._log_line("[UI] Historique supprimé.")
        except Exception as e:
            self._log_line(f"[UI] erreur suppression historique: {e}")

    def export_table(self):
        if not self.tree.get_children():
            messagebox.showinfo("Export", "Tableau vide.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")],
            title="Exporter les News (TE + CB)"
        )
        if not path:
            return

        try:
            rows = []
            for iid in self.tree.get_children():
                rows.append(self.tree.item(iid, "values"))

            if path.lower().endswith(".csv"):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Time","Source","Country","Currency","Event","Actual","Consensus","Previous","Imp."])
                    for r in rows:
                        writer.writerow(r)
            else:
                keys = ["time","source","country","currency","event","actual","consensus","previous","importance"]
                data = [dict(zip(keys, r)) for r in rows]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

            self._log_line(f"[UI] Exporté: {path}")
        except Exception as e:
            self._log_line(f"[UI] export_error: {e}")

    # --------------------------------------------------------------- Logs
    def _log_line(self, text: str):
        self.txt_logs.configure(state="normal")
        self.txt_logs.insert("end", text + "\n")
        self.txt_logs.see("end")
        self.txt_logs.configure(state="disabled")
        self._log_lines += 1
        if self._log_lines > 5000:
            self.clean_logs()


# ------------------------------- Placeholder tabs -------------------------------

class PlaceholderTab(ttk.Frame):
    def __init__(self, master, title):
        super().__init__(master)
        f = ttk.Frame(self)
        f.pack(expand=True, fill="both")
        lbl = ttk.Label(f, text=f"{title} — (placeholder)", anchor="center")
        lbl.place(relx=0.5, rely=0.5, anchor="center")


# =================================== App ===================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x780")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        # Crée d'abord Dashboard, puis News (News a besoin d'un handle sur Dashboard)
        self.tab_dashboard = DashboardTab(notebook)
        self.tab_news = NewsTab(notebook, dashboard=self.tab_dashboard)

        notebook.add(self.tab_dashboard, text="Dashboard")
        notebook.add(self.tab_news, text="News")

        # placeholders pour garder ta structure ; tu peux les retirer si inutile
        notebook.add(PlaceholderTab(notebook, "Data"), text="Data")
        notebook.add(PlaceholderTab(notebook, "Historique"), text="Historique")
        notebook.add(PlaceholderTab(notebook, "Démo"), text="Démo")
        notebook.add(PlaceholderTab(notebook, "Logs"), text="Logs")


if __name__ == "__main__":
    App().mainloop()
