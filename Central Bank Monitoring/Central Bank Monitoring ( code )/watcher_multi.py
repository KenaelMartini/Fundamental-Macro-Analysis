# file: watcher_multi.py
from __future__ import annotations
from tkinter import filedialog, messagebox  # (laissé si tu en as besoin ailleurs)

import threading
import csv
import os
import sys
import time
import argparse
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl
from collections import deque

# --- Sources (tu dois avoir cb_sources.py avec les classes WebSource / BOE, etc.)
from cb_sources import BOE, FED, ECB, BOJ, BOC, RBA, RBNZ, SNB, WebSource

# Watcher macro (TradingEconomics)
try:
    from econ_watcher import EconWatcher
    HAS_ECON = True
except Exception:
    HAS_ECON = False

# NLP (affichage quand on déclenche)
try:
    from nlp_advanced import advanced_analyze, pretty_print_result
    HAS_NLP = True
except Exception:
    HAS_NLP = False

# Affichage “terminal”
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    HAS_RICH = True
    _console = Console()
except Exception:
    HAS_RICH = False
    _console = None


# ======================================================================================
# CONFIG
# ======================================================================================

DEFAULT_POLL = float(os.getenv("CBM_POLL", "0.025"))        # 25 ms comme avant
DEFAULT_COOLDOWN = int(os.getenv("CBM_COOLDOWN", "60"))
K_CONTEXT = int(os.getenv("CBM_CONTEXT_K", "5"))

# Au boot, on pré-marque les derniers items comme "déjà vus"
WARM_SEED = int(os.getenv("CBM_WARM_SEED", "20"))

# Métriques/HB
HB_AGG_PERIOD = float(os.getenv("CBM_HB_AGG_SEC", "2.0"))   # heartbeat agrégé
HB_BANK_PERIOD = float(os.getenv("CBM_HB_BANK_SEC", "2.0")) # heartbeat par banque

# États anti-réexécution
LAST_FINGERPRINT: dict[str, str] = {}
LAST_RUN_TS: dict[str, float] = {}
SEEN_KEYS: dict[str, deque[str]] = {}
SEEN_MAX = 512  # mémoire des items déjà traités

# Libellés "jolis" pour le Dashboard
BANK_LABELS = {
    "boe": "BoE", "fed": "Fed", "ecb": "ECB", "boj": "BoJ",
    "boc": "BoC", "rba": "RBA", "rbnz": "RBNZ", "snb": "SNB"
}


# ======================================================================================
# OUTILS / AFFICHAGE
# ======================================================================================

def _canon_link(url: str) -> str:
    try:
        p = urlsplit(url)
        kept = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            kl = k.lower()
            if kl.startswith(("utm_", "gclid", "fbclid", "t")):
                continue
            kept.append((k, v))
        new_q = "&".join(f"{k}={v}" for k, v in sorted(kept))
        new_path = p.path.rstrip("/")
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), new_path, new_q, ""))
    except Exception:
        return (url or "").strip().lower()


def _fingerprint(meta: dict) -> str:
    link = _canon_link(meta.get("link", ""))
    title = (meta.get("title") or "").strip().lower()
    return f"{link}|{title}"


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def print_header(banks: list[tuple[str, WebSource]], poll: float, cooldown: int, k_context: int):
    title = "Démarrage — Multi Central Banks"
    if HAS_RICH:
        table = Table.grid(expand=True)
        table.add_column(justify="left", ratio=1)
        for bid, bank in banks:
            table.add_row(
                f"[white]  {bank.name} ({BANK_LABELS.get(bid, bid)}) — "
                f"Poll={poll:.3f}s, Cooldown={cooldown}s, ContextK={k_context}[/]"
            )
        _console.print(Panel.fit(table, title=title, border_style="cyan"))
    else:
        print("─" * 25, title, "─" * 25)
        for bid, bank in banks:
            print(f"|  {bank.name} ({BANK_LABELS.get(bid, bid)}) — "
                  f"Poll={poll:.3f}s, Cooldown={cooldown}s, ContextK={k_context}")
        print("─" * 75)


def print_heartbeat_aggregate():
    # Ligne agrégée simple (utile en console + fallback UI)
    print(f" Heartbeat OK — {_utc_now_str()}", flush=True)


def print_hb_line(source: str, *, status="ok",
                  latency_ms: int | None = None,
                  mode: str | None = None,
                  notes: str | None = None):
    """
    Construit une ligne HB structurée que le Dashboard sait parser:
    [HB] alive source=Fed status=ok latency=25ms mode=poll=0.025s notes=...
    """
    parts = [f"[HB] alive source={source}", f"status={status}"]
    if latency_ms is not None:
        parts.append(f"latency={latency_ms}ms")
    if mode:
        parts.append(f"mode={mode}")
    if notes:
        parts.append(f"notes={notes}")
    print(" ".join(parts), flush=True)


# ======================================================================================
# ANALYSE
# ======================================================================================

def _safe_pretty_print(adv: dict, *, items_for_context: list[dict] | None):
    try:
        ctx_bullets = []
        if items_for_context:
            for it in items_for_context:
                ctx_bullets.append({
                    "date": it.get("date") or it.get("pubDate") or "—",
                    "title": it.get("title") or "—",
                    "link": it.get("link") or "—",
                })
        pretty_print_result(
            adv,
            suppress_model_sources=True,
            context_bullets=ctx_bullets
        )
    except Exception:
        pass


def _run_pipeline(bank_id: str,
                  bank_name: str,
                  meta: dict,
                  items_for_context: list[dict] | None = None):
    """
    Lance l'analyse avancée, pousse les lignes [DATA] et [ANALYSIS], et affiche (optionnel).
    """
    title = meta.get("title") or "—"
    link = meta.get("link") or "—"
    date = meta.get("pubDate") or "—"

    header = f"{bank_name} — Nouvelle analyse"
    if HAS_RICH:
        head = Table.grid(expand=True)
        head.add_column(justify="left", ratio=1)
        head.add_row(f"[bold]{header}[/]")
        _console.print(Panel(head, border_style="green"))
        info = Table.grid()
        info.add_column(ratio=1, justify="left")
        info.add_row(f"[white]Titre :[/] {title}")
        info.add_row(f"[white]Date  :[/] {date}")
        info.add_row(f"[white]Lien  :[/] {link}")
        _console.print(info)
    else:
        print(f"{'─' * 40} {header} {'─' * 40}")
        print(f"Titre : {title}")
        print(f"Date  : {date}")
        print(f"Lien  : {link}")

    # NLP ou fallback
    if not HAS_NLP:
        bank_code = BANK_LABELS.get(bank_id.lower(), bank_name)
        print(f"[DATA] {title} source={bank_code} link={link}", flush=True)

        payload = {
            "bank_id": bank_id,
            "bank": bank_name,
            "title": title,
            "link": link,
            "pubDate": date,
            "analysis": {
                "tone": "Neutre",
                "relative_tone": "Conforme",
                "labels": ["inflation", "croissance"],
                "scores": [0.7, 0.3],
                "summary": "Résumé fictif de la décision.",
                "insights": ["Impact modéré attendu", "Peu de surprises dans le discours"]
            },
            "sources": [link] if link else [],
            "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }
        import json
        print("[ANALYSIS] " + json.dumps(payload, ensure_ascii=False), flush=True)
        return

    # NLP OK
    raw_text = f"{title}"
    adv = advanced_analyze(
        raw_text,
        bank=bank_name,
        context_items=items_for_context or []
    )

    _safe_pretty_print(adv, items_for_context=items_for_context)

    sources = []
    if link and isinstance(link, str):
        sources.append(link)
    if items_for_context:
        for it in items_for_context:
            L = (it.get("link") or "").strip()
            if L:
                sources.append(L)

    payload = {
        "bank_id": bank_id,
        "bank": bank_name,
        "title": title,
        "link": link,
        "pubDate": date,
        "analysis": adv,
        "sources": sources,
        "ts": _utc_now_str(),
    }

    bank_code = BANK_LABELS.get(bank_id.lower(), bank_name)
    print(f"[DATA] {title} source={bank_code} link={link}", flush=True)
    import json
    print("[ANALYSIS] " + json.dumps(payload, ensure_ascii=False), flush=True)


# ======================================================================================
# DÉTECTION
# ======================================================================================

def start_all_watchers(demo: bool):
    threads = []

    # === Watcher macro (TradingEconomics) ===
    if HAS_ECON:
        econ = EconWatcher(cfg_path="config/econ_te.yaml", heartbeat_sec=300, rate_per_sec=10)
        t_econ = threading.Thread(target=econ.run_forever, daemon=True)
        t_econ.start()
        threads.append(t_econ)

    for t in threads:
        t.join()


def _seed_seen_for_bank(bid: str, bank: WebSource, seed_n: int):
    SEEN_KEYS.setdefault(bid, deque(maxlen=SEEN_MAX))
    try:
        recent = bank.fetch_recent_meta(n=seed_n) or []
    except Exception:
        recent = []

    for it in recent:
        key = _fingerprint(it)
        if key and key not in SEEN_KEYS[bid]:
            SEEN_KEYS[bid].append(key)

    if recent:
        LAST_FINGERPRINT[bid] = _fingerprint(recent[0])
        LAST_RUN_TS[bid] = time.time()


def _maybe_analyze_bank(bid: str,
                        bank: WebSource,
                        *,
                        k_context: int,
                        cooldown_sec: int):
    SEEN_KEYS.setdefault(bid, deque(maxlen=SEEN_MAX))
    try:
        meta = bank.fetch_latest_meta()
    except Exception:
        return
    if not meta or not meta.get("link"):
        return

    key = _fingerprint(meta)
    now = time.time()

    if key in SEEN_KEYS[bid]:
        LAST_FINGERPRINT[bid] = key
        return
    if key == LAST_FINGERPRINT.get(bid):
        return
    last_ts = LAST_RUN_TS.get(bid, 0.0)
    if now - last_ts < cooldown_sec:
        return

    LAST_FINGERPRINT[bid] = key
    LAST_RUN_TS[bid] = now
    SEEN_KEYS[bid].append(key)

    try:
        metas = bank.fetch_recent_meta(n=k_context) or []
    except Exception:
        metas = []
    items_for_context = [
        {"date": it.get("pubDate") or "—", "title": it.get("title") or "—", "link": it.get("link") or "—"}
        for it in metas[:k_context]
    ]

    _run_pipeline(bid, bank.name, meta, items_for_context=items_for_context)


# ======================================================================================
# MAIN
# ======================================================================================

ALL_BANKS: dict[str, WebSource] = {
    "boe": BOE, "fed": FED, "ecb": ECB, "boj": BOJ,
    "boc": BOC, "rba": RBA, "rbnz": RBNZ, "snb": SNB,
}

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Central Bank Multi Watcher")
    p.add_argument("--banks", type=str, default="all",
                   help="IDs séparés par virgules (ex: boe,fed,ecb) ou 'all'")
    p.add_argument("--k", type=int, default=K_CONTEXT, help="K derniers items pour le contexte")
    p.add_argument("--poll", type=float, default=DEFAULT_POLL, help="Intervalle de poll (s)")
    p.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN, help="Cooldown entre analyses (s)")
    p.add_argument("--demo", action="store_true", help="Injecte des publications factices pour tester l’UI")
    return p.parse_args(argv)


def main():
    args = parse_args()
    demo = args.demo

    if args.banks.strip().lower() == "all":
        selected: list[tuple[str, WebSource]] = list(ALL_BANKS.items())
    else:
        ids = [x.strip().lower() for x in args.banks.split(",") if x.strip()]
        selected = [(bid, ALL_BANKS[bid]) for bid in ids if bid in ALL_BANKS]
        if not selected:
            print("Aucune banque valide. IDs possibles:", ", ".join(ALL_BANKS.keys()))
            sys.exit(1)

    print_header(selected, poll=args.poll, cooldown=args.cooldown, k_context=args.k)

    # Drapeaux HB
    last_hb_agg = 0.0
    last_hb_bank: dict[str, float] = {BANK_LABELS.get(bid, bid): 0.0 for bid, _ in selected}
    nominal_latency_ms = max(1, int(round(args.poll * 1000)))  # ex: 25 ms

    # Boot: annonce watcher + premières lignes HB par banque (mode)
    print("[HB] watcher actif…", flush=True)
    mode_str = f"poll={args.poll:.3f}s,cooldown={args.cooldown}s,k={args.k}"
    for bid, _ in selected:
        label = BANK_LABELS.get(bid, bid)
        print_hb_line(label, status="ok", latency_ms=nominal_latency_ms, mode=mode_str)

    # Warm seed
    for bid, bank in selected:
        _seed_seen_for_bank(bid, bank, seed_n=WARM_SEED)

    # Démarrer le watcher macro TradingEconomics si dispo
    if HAS_ECON:
        t = threading.Thread(target=start_all_watchers, args=(demo,), daemon=True)
        t.start()

    demo_fired_1 = False
    demo_fired_2 = False
    start_time = time.time()

    try:
        while True:
            now = time.time()

            # Heartbeat agrégé
            if now - last_hb_agg >= HB_AGG_PERIOD:
                print_heartbeat_aggregate()
                last_hb_agg = now

            # Poll & HB par banque
            for bid, bank in selected:
                _maybe_analyze_bank(
                    bid,
                    bank,
                    k_context=args.k,
                    cooldown_sec=args.cooldown
                )

                # Heartbeat “vivant” par banque (toutes les ~2s)
                label = BANK_LABELS.get(bid, bid)
                if now - last_hb_bank[label] >= HB_BANK_PERIOD:
                    print_hb_line(label, status="ok", latency_ms=nominal_latency_ms)
                    last_hb_bank[label] = now

            # DEMO injections
            if demo and not demo_fired_1 and (now - start_time) >= 3.0:
                meta = {
                    "title": "Bank Rate reduced to 4% - August 2025 (DEMO)",
                    "link": "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2025/august-2025",
                    "pubDate": _utc_now_str(),
                }
                _run_pipeline("boe", "Bank of England", meta, items_for_context=[])
                print(f"[DATA] MPC Statement released (demo) source=BoE link={meta['link']}", flush=True)
                print_hb_line("BoE", status="ok", latency_ms=nominal_latency_ms, notes="demo_event")
                demo_fired_1 = True

            if demo and not demo_fired_2 and (now - start_time) >= 6.0:
                meta2 = {
                    "title": "ECB monetary policy decision (DEMO)",
                    "link": "https://www.ecb.europa.eu/press/pr/date/2025/html/index.en.html",
                    "pubDate": _utc_now_str(),
                }
                _run_pipeline("ecb", "European Central Bank", meta2, items_for_context=[])
                print(f"[DATA] Press release (demo) source=ECB link={meta2['link']}", flush=True)
                print_hb_line("ECB", status="ok", latency_ms=nominal_latency_ms, notes="demo_event")
                demo_fired_2 = True

            time.sleep(max(0.05, args.poll))
    except KeyboardInterrupt:
        print("[HB] stop source=CB reason=KeyboardInterrupt", flush=True)
    finally:
        print("[HB] info source=CB reader_done", flush=True)
        print("[MultiWatcher] Arrêt demandé. Fin.", flush=True)


if __name__ == "__main__":
    main()
