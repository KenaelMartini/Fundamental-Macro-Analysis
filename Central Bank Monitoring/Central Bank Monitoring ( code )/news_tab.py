# -*- coding: utf-8 -*-
# news_tab.py — Onglet "News" pour lancer watcher_te_calendar.py et afficher les events

import os, sys, json, threading, queue, subprocess, shlex, time
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext

MAX_ROWS = 500  # limiter les lignes visibles dans le tableau
LOG_RING = 500  # lignes maxi dans le log

class NewsTab(ttk.Frame):
    def __init__(self, master, script_filename="watcher_te_calendar.py", history_path="history_news.jsonl"):
        super().__init__(master)

        # Résolution du chemin du watcher (même répertoire que ui_window.py)
        base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        candidate1 = os.path.join(base_dir, script_filename)
        candidate2 = os.path.join(os.path.dirname(__file__), script_filename)
        self.script_path = candidate1 if os.path.exists(candidate1) else candidate2

        self.history_path = os.path.join(base_dir, history_path)
        self.proc = None
        self.reader_thread = None
        self.q = queue.Queue()
        self._running = False
        self._expect_json_for = None  # "DATA" ou "ANALYSIS"
        self._row_by_event = {}  # event_id -> iid Treeview
        self._log_lines = []

        # ---------- BARRE DE CONTROLES ----------
        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=6)

        # Source flags
        self.var_use_ff = tk.BooleanVar(value=True)
        self.var_use_api = tk.BooleanVar(value=False)
        self.var_api_strict = tk.BooleanVar(value=False)
        self.var_fx8 = tk.BooleanVar(value=True)
        self.var_emit_initial = tk.BooleanVar(value=True)
        self.var_test = tk.BooleanVar(value=False)
        self.var_demo_fx8 = tk.BooleanVar(value=False)
        self.var_verbose = tk.BooleanVar(value=True)

        chk_frame = ttk.Frame(controls)
        chk_frame.pack(side="left", padx=4)
        ttk.Checkbutton(chk_frame, text="FF (fallback)", variable=self.var_use_ff).grid(row=0, column=0, sticky="w", padx=2)
        ttk.Checkbutton(chk_frame, text="API TE", variable=self.var_use_api).grid(row=0, column=1, sticky="w", padx=2)
        ttk.Checkbutton(chk_frame, text="API strict/pays", variable=self.var_api_strict).grid(row=0, column=2, sticky="w", padx=2)
        ttk.Checkbutton(chk_frame, text="FX8", variable=self.var_fx8).grid(row=0, column=3, sticky="w", padx=2)
        ttk.Checkbutton(chk_frame, text="Emit initial", variable=self.var_emit_initial).grid(row=0, column=4, sticky="w", padx=2)
        ttk.Checkbutton(chk_frame, text="Test (force actual)", variable=self.var_test).grid(row=0, column=5, sticky="w", padx=2)
        ttk.Checkbutton(chk_frame, text="Demo FX8", variable=self.var_demo_fx8).grid(row=0, column=6, sticky="w", padx=2)
        ttk.Checkbutton(chk_frame, text="Verbose", variable=self.var_verbose).grid(row=0, column=7, sticky="w", padx=2)

        # Params
        param_frame = ttk.Frame(controls)
        param_frame.pack(side="left", padx=8)

        ttk.Label(param_frame, text="Days:").grid(row=0, column=0, sticky="e")
        self.var_days = tk.StringVar(value="1")
        ttk.Entry(param_frame, textvariable=self.var_days, width=4).grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(param_frame, text="Importance:").grid(row=0, column=2, sticky="e")
        self.var_importance = tk.StringVar(value="1,2,3")
        ttk.Entry(param_frame, textvariable=self.var_importance, width=8).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(param_frame, text="Countries:").grid(row=0, column=4, sticky="e")
        self.var_countries = tk.StringVar(value="United States,Euro Area,United Kingdom,Japan,Canada,Australia,New Zealand,Switzerland")
        ttk.Entry(param_frame, textvariable=self.var_countries, width=70).grid(row=0, column=5, sticky="w", padx=4)

        ttk.Label(param_frame, text="Allow CCY:").grid(row=0, column=6, sticky="e")
        self.var_allow_currencies = tk.StringVar(value="")
        ttk.Entry(param_frame, textvariable=self.var_allow_currencies, width=12).grid(row=0, column=7, sticky="w", padx=4)

        # TE key (optionnel)
        ttk.Label(param_frame, text="TE key:").grid(row=1, column=0, sticky="e", pady=2)
        self.var_te_key = tk.StringVar(value=os.getenv("TE_API_KEY", ""))  # auto-prérempli si var d'env
        ttk.Entry(param_frame, textvariable=self.var_te_key, width=24, show="*").grid(row=1, column=1, sticky="w", padx=4, pady=2)

        # Boutons Start/Stop
        btn_frame = ttk.Frame(controls)
        btn_frame.pack(side="right", padx=4)
        self.btn_start = ttk.Button(btn_frame, text="▶ Lancer watcher", command=self.start_watcher)
        self.btn_start.grid(row=0, column=0, padx=4)
        self.btn_stop = ttk.Button(btn_frame, text="■ Stop", command=self.stop_watcher, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=4)

        # ---------- TABLE ----------
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=6, pady=(0,6))

        cols = ("time","country","currency","event","actual","consensus","previous","importance")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=14)
        for c, w in [("time",160),("country",140),("currency",70),("event",320),
                     ("actual",80),("consensus",90),("previous",80),("importance",90)]:
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")

        # tags visuelles
        style = ttk.Style(self)
        # Important en rouge (selon thème)
        self.tree.tag_configure("important", background="#ffe5e5")
        self.tree.tag_configure("medium", background="#fff6e5")
        self.tree.tag_configure("low", background="")

        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        # ---------- LOG ----------
        log_frame = ttk.LabelFrame(self, text="Logs")
        log_frame.pack(fill="both", expand=False, padx=6, pady=(0,6))
        self.log = scrolledtext.ScrolledText(log_frame, height=10, wrap="word")
        self.log.pack(fill="both", expand=True)
        self.status = ttk.Label(self, text="Prêt.", anchor="w")
        self.status.pack(fill="x")

        # Scheduler pour vider la file
        self.after(60, self._drain_queue)

        # Nettoyage à la fermeture de la fenêtre
        self.winfo_toplevel().protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Command line ----------
    def _build_command(self):
        if not os.path.exists(self.script_path):
            raise FileNotFoundError(f"watcher introuvable : {self.script_path}")

        args = [sys.executable, self.script_path]

        if self.var_use_ff.get():
            args += ["--ff"]

        if self.var_use_api.get():
            args += ["--api"]
            if self.var_api_strict.get():
                args += ["--api-strict-countries"]
            te_key = (self.var_te_key.get() or "").strip()
            if te_key:
                args += ["--te-key", te_key]

        if self.var_fx8.get():
            args += ["--fx8"]
        if self.var_emit_initial.get():
            args += ["--emit-initial"]
        if self.var_test.get():
            args += ["--test"]
        if self.var_demo_fx8.get():
            args += ["--demo-fx8"]
        if self.var_verbose.get():
            args += ["--verbose"]

        days = (self.var_days.get() or "").strip()
        if days:
            args += ["--days", days]

        imp = (self.var_importance.get() or "").strip()
        if imp:
            args += ["--importance", imp]

        countries = (self.var_countries.get() or "").strip()
        if countries:
            args += ["--countries", countries]

        allow_ccy = (self.var_allow_currencies.get() or "").strip()
        if allow_ccy:
            args += ["--allow-currencies", allow_ccy]

        return args

    # ---------- Start / Stop ----------
    def start_watcher(self):
        if self.proc:
            self._log_line("Watcher déjà en cours.")
            return
        try:
            cmd = self._build_command()
        except Exception as e:
            self._log_line(f"[UI] erreur build cmd: {e}")
            return

        self._log_line("[UI] Lancement: " + " ".join(shlex.quote(x) for x in cmd))
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
        except Exception as e:
            self._log_line(f"[UI] erreur lancement: {e}")
            self.proc = None
            return

        self._running = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.configure(text="Watcher démarré.")
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def stop_watcher(self):
        if not self.proc:
            return
        self._running = False
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.proc = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status.configure(text="Watcher arrêté.")
        self._log_line("[UI] Watcher arrêté.")

    # ---------- Reader / Queue ----------
    def _reader_loop(self):
        # lit stdout ligne par ligne
        try:
            for line in self.proc.stdout:
                if not self._running:
                    break
                self.q.put(line.rstrip("\n"))
        except Exception as e:
            self.q.put(f"[UI] reader_error: {e}")
        finally:
            self.q.put("[UI] reader_done")

    def _drain_queue(self):
        try:
            while True:
                line = self.q.get_nowait()
                self._handle_line(line)
        except queue.Empty:
            pass
        if self._running:
            self.after(60, self._drain_queue)

    # ---------- Parsing des lignes watcher ----------
    def _handle_line(self, line: str):
        # Détection des balises
        if line.startswith("[DATA]"):
            self._expect_json_for = "DATA"
            return
        if line.startswith("[ANALYSIS]"):
            self._expect_json_for = "ANALYSIS"
            return
        if line.startswith("[HB]"):
            self._append_hb(line)
            return
        if line.startswith("[UI]"):
            self._log_line(line)
            return

        # JSON attendu après [DATA] / [ANALYSIS]
        if self._expect_json_for:
            try:
                obj = json.loads(line)
                if self._expect_json_for == "DATA":
                    self._on_data(obj)
                else:
                    self._on_analysis(obj)
            except Exception as e:
                self._log_line(f"[UI] JSON error ({self._expect_json_for}): {e}  line={line[:200]}")
            finally:
                self._expect_json_for = None
            return

        # Sinon, log brut
        self._log_line(line)

    # ---------- UI updates ----------
    def _on_data(self, ev: dict):
        # Sauvegarde JSONL
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log_line(f"[UI] write history error: {e}")

        # Insère ou met à jour la ligne
        event_id = ev.get("event_id") or f"tmp:{time.time_ns()}"
        time_iso = ev.get("calendar_time") or ev.get("published_at") or ""
        country = ev.get("country") or ""
        currency = ev.get("currency") or ""
        event = ev.get("event") or ""
        actual = self._fmt_num(ev.get("actual"))
        consensus = self._fmt_num(ev.get("consensus"))
        previous = self._fmt_num(ev.get("previous"))
        imp = ev.get("importance")

        # Tag visuel provisoire d'après actual/consensus
        tag = self._compute_tag(ev)

        row_vals = (time_iso, country, currency, event, actual, consensus, previous, imp)
        iid = self._row_by_event.get(event_id)
        if iid and iid in self.tree.get_children(""):
            self.tree.item(iid, values=row_vals, tags=(tag,))
        else:
            if len(self.tree.get_children("")) >= MAX_ROWS:
                # drop le plus ancien
                oldest = self.tree.get_children("")[0]
                self.tree.delete(oldest)
            iid = self.tree.insert("", "end", values=row_vals, tags=(tag,))
            self._row_by_event[event_id] = iid

    def _on_analysis(self, an: dict):
        # Rehausse la ligne si "important" dans labels
        eid = an.get("event_id")
        labels = an.get("labels") or []
        tag = "important" if ("important" in [x.lower() for x in labels]) else (an.get("impact") or "low")
        iid = self._row_by_event.get(eid)
        if iid and iid in self.tree.get_children(""):
            vals = self.tree.item(iid, "values")
            self.tree.item(iid, values=vals, tags=(tag,))

    def _append_hb(self, line: str):
        # Heartbeats vers le log + status court
        self._log_line(line)
        # status court si latence présente
        if "latency=" in line:
            try:
                lat = line.split("latency=")[1].split("ms")[0]
                self.status.configure(text=f"HB ok — {lat} ms")
            except Exception:
                pass

    def _log_line(self, s: str):
        # ring buffer
        self._log_lines.append(s)
        if len(self._log_lines) > LOG_RING:
            self._log_lines = self._log_lines[-LOG_RING:]
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(self._log_lines) + "\n")
        self.log.see("end")

    # ---------- Utils ----------
    @staticmethod
    def _fmt_num(x):
        if x is None:
            return ""
        try:
            # affiche 2 décimales si float
            if isinstance(x, (int, float)):
                return f"{x:.2f}".rstrip("0").rstrip(".")
            return str(x)
        except Exception:
            return str(x)

    @staticmethod
    def _compute_tag(ev: dict):
        try:
            imp = int(ev.get("importance") or 0)
            a = ev.get("actual"); c = ev.get("consensus")
            if a is not None and c is not None:
                try:
                    diff = abs(float(a) - float(c))
                    if imp >= 3 and diff >= 0.2:
                        return "important"
                    if diff >= 0.1:
                        return "medium"
                except Exception:
                    pass
            return "low"
        except Exception:
            return "low"

    # ---------- Shutdown ----------
    def _on_close(self):
        # appelé quand la fenêtre principale se ferme
        self.stop_watcher()
        self.winfo_toplevel().destroy()
