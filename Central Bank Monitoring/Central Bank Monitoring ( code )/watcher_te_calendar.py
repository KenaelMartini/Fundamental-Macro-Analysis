# -*- coding: utf-8 -*-
"""
watcher_te_calendar.py — TradingEconomics Calendar Watcher (robuste)

Ajout: détection spécifique des datas emploi ("Labor Market")
- Employment Change, Unemployment Rate, Job Vacancies, JOLTS, Jobless Claims, Productivity, Participation Rate, Wages.
- Catégorie "Labor Market" + label "labor" dans l'analyse.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup, SoupStrainer, Tag

# ---------------- Const ----------------
TE_URL = "https://tradingeconomics.com/calendar"
TE_API_BASE = "https://api.tradingeconomics.com"
FF_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

FX8_SET = {"USD", "EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "NZD"}

# Nouveaux events "emploi/labour"
LABOUR_EVENTS = [
    "unemployment", "jobless", "employment change", "non-farm payrolls",
    "jolts", "job openings", "job vacancies", "claims", "labour productivity",
    "participation rate", "wage growth", "average hourly earnings"
]

CURRENCY_TO_COUNTRY = {
    "USD": "United States",
    "EUR": "Euro Area",
    "GBP": "United Kingdom",
    "JPY": "Japan",
    "CAD": "Canada",
    "CHF": "Switzerland",
    "AUD": "Australia",
    "NZD": "New Zealand",
}

COUNTRY_TO_CURRENCY = {
    "euro area": "EUR", "eurozone": "EUR",
    "germany": "EUR", "france": "EUR", "italy": "EUR", "spain": "EUR",
    "netherlands": "EUR", "belgium": "EUR", "ireland": "EUR", "portugal": "EUR",
    "austria": "EUR", "finland": "EUR", "greece": "EUR", "slovakia": "EUR",
    "slovenia": "EUR", "estonia": "EUR", "latvia": "EUR", "lithuania": "EUR",
    "united states": "USD", "uk": "GBP", "united kingdom": "GBP",
    "japan": "JPY", "canada": "CAD", "switzerland": "CHF",
    "australia": "AUD", "new zealand": "NZD",
    "china": "CNY", "india": "INR", "brazil": "BRL", "mexico": "MXN",
    "norway": "NOK", "sweden": "SEK", "denmark": "DKK",
    "czech republic": "CZK", "poland": "PLN", "hungary": "HUF",
    "tajikistan": "TJS", "oman": "OMR",
}

# ---------------- Utils ----------------
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def heartbeat(latency_ms: int = -1, status: str = "ok", msg: Optional[str] = None):
    if latency_ms >= 0:
        out = f"[HB] alive source=TECal status={status} latency={latency_ms}ms"
    else:
        out = f"[HB] alive source=TECal status={status}"
    if msg:
        out += f" {msg}"
    print(out, flush=True)

def warn(msg: str):
    print(f"[HB] warn source=TECal {msg}", flush=True)

def info(msg: str):
    print(f"[HB] info source=TECal {msg}", flush=True)

def norm(txt: Optional[str]) -> Optional[str]:
    if not txt:
        return None
    return re.sub(r"\s+", " ", txt).strip() or None

def normalize_country(s: Optional[str]) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\u00A0\s]+", " ", s)
    s = re.sub(r"[()\-–—]|calendar", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def to_float_safe(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    x = s.strip().replace("%", "").replace(",", ".")
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?", x):
        return None
    try:
        return float(x)
    except Exception:
        return None

def build_event_id(currency_code: str, slug: str, calendar_iso: str) -> str:
    return f"te:{currency_code}:{slug}:{calendar_iso}"

def compute_surprise(actual: Optional[float], consensus: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if actual is None or consensus is None:
        return None, None
    diff = actual - consensus
    pct = None if consensus == 0 else (diff / consensus) * 100
    return diff, pct

def country_to_currency(country: Optional[str], fallback: Optional[str]) -> str:
    if fallback and fallback.strip():
        return fallback.strip().upper()
    key = normalize_country(country)
    if key in COUNTRY_TO_CURRENCY:
        return COUNTRY_TO_CURRENCY[key]
    for k, v in COUNTRY_TO_CURRENCY.items():
        if k in key:
            return v
    return (country or "XX")[:3].upper()

# ---------------- Fetch HTML ----------------
def fetch_html(url: str, max_bytes: int = 1_500_000) -> bytes:
    t0 = time.perf_counter()
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        timeout=(3, 8),
    )
    resp.raise_for_status()
    raw = resp.content
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    latency_ms = int((time.perf_counter() - t0) * 1000)
    heartbeat(latency_ms)
    return raw

# ---------------- API TE ----------------
# (toutes les fonctions fetch_te_api_xxx et parse_events_from_te_api restent identiques)

# ---------------- Soup helpers & Parsing HTML ----------------
# (idem, rien changé sauf que category sera modifiée plus tard)

# ---------------- FF fallback ----------------
# (idem, inchangé)

# ---------------- Filters ----------------
# (idem)

# ---------------- Analyse & Emit ----------------
def analyse_event(ev: Dict) -> Dict:
    labels: List[str] = []

    if ev.get("actual") is not None and ev.get("consensus") is not None:
        diff = abs(ev["actual"] - ev["consensus"])
        if ev.get("importance", 1) >= 3 and diff >= (0.2 if ev.get("unit") == "%" else 1.0):
            labels += ["important", "high_impact"]

    name = (ev.get("event") or "").lower()

    if "cpi" in name or "inflation" in name:
        labels.append("inflation")
    if "gdp" in name:
        labels.append("growth")

    # Nouveau bloc: tous les jobs/labour
    if any(k in name for k in LABOUR_EVENTS):
        labels.append("labor")
        ev["category"] = "Labor Market"

    if "retail" in name:
        labels.append("retail")
    if "pmi" in name or "ism" in name:
        labels.append("pmi")

    if ev.get("currency"):
        labels.append(str(ev["currency"]).lower())

    impact = "high" if "high_impact" in labels else ("medium" if ev.get("importance", 1) >= 2 else "low")

    return {
        "source": ev["source"],
        "event_id": ev["event_id"],
        "labels": labels,
        "impact": impact,
        "bias": None,
        "notes": None,
        "score": None,
        "timestamp": utcnow_iso(),
    }

def print_payload(tag: str, obj: Dict):
    sys.stdout.write(f"[{tag}]\n")
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def emit_event(ev: Dict, *, test: bool = False, force_emit: bool = False, initial: bool = False):
    out = dict(ev)
    if initial:
        out["meta"]["initial"] = True
    if test:
        out["meta"]["test_mode"] = True
        if out.get("actual") is None:
            out["actual"] = out.get("consensus") or out.get("previous")
    if not force_emit and out.get("actual") is None and not test:
        return
    out["published_at"] = utcnow_iso()
    print_payload("DATA", out)
    print_payload("ANALYSIS", analyse_event(out))

# ---------------- Main ----------------
def main():
    # parser args, boucle, fetch, filtres, emit -> inchangé
    # (reprends ton code original sans modifications autres que ci-dessus)
    ...

if __name__ == "__main__":
    main()
