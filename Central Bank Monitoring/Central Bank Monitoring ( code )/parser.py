# file: parser.py
from __future__ import annotations

import os
import re
import asyncio
from typing import Optional
from urllib.parse import urlparse

import requests

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

# Optional headless fallback for sites that block plain requests (e.g., RBNZ)
try:
    from pyppeteer import launch
    HAS_PYPPETEER = True
except Exception:
    HAS_PYPPETEER = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "close",
}

NOISY_LINES = (
    "nothing searched for.",
    "official websites use .gov",
    "secure .gov websites use https",
    "cookies",
    "newsletter",
    "subscribe to rss",
)

def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    out = []
    for ln in raw.splitlines():
        low = ln.strip().lower()
        if any(token in low for token in NOISY_LINES):
            continue
        if not ln.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln.rstrip())
    txt = "\n".join(out)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt

def _is_rbnz(url: str) -> bool:
    try:
        return "rbnz.govt.nz" in (urlparse(url).netloc or "").lower()
    except Exception:
        return False

# ---------- local browser detection (Windows) ----------
_LOCAL_BROWSER_PATH: Optional[str] = None
def _find_local_browser() -> Optional[str]:
    global _LOCAL_BROWSER_PATH
    if _LOCAL_BROWSER_PATH is not None:
        return _LOCAL_BROWSER_PATH or None
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            _LOCAL_BROWSER_PATH = p
            return p
    _LOCAL_BROWSER_PATH = ""   # cache negative result
    return None

async def _fetch_html_with_pyppeteer(url: str, timeout_ms: int = 25000) -> str:
    exe = _find_local_browser()
    if not exe:
        raise RuntimeError("No local Chrome/Edge found; skip headless fallback to avoid Chromium download.")
    browser = await launch(
        executablePath=exe,
        headless=True,
        handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    try:
        page = await browser.newPage()
        await page.setUserAgent(HEADERS["User-Agent"])
        await page.setExtraHTTPHeaders({"Accept-Language": HEADERS["Accept-Language"]})
        await page.goto(url, {"waitUntil": "networkidle2", "timeout": timeout_ms})
        try:
            await page.waitForSelector("main, article, #content, .content, .article", {"timeout": 5000})
        except Exception:
            pass
        html = await page.content()
        return html or ""
    finally:
        await browser.close()

def _extract_visible_text(html: str) -> str:
    if not HAS_BS4 or not html:
        return html or ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    candidates = []
    for sel in ("main", "article", "#content", ".content", ".article", ".post", ".prose"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 100:
            candidates.append(el)
    if not candidates:
        candidates = [soup.find("body") or soup]
    best = max(candidates, key=lambda el: len(el.get_text(" ", strip=True)))
    text = best.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def _requests_get_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if any(bin_hint in ctype for bin_hint in ("application/pdf", "application/octet-stream", "application/vnd", "zip", "msword", "spreadsheet")):
        return ""
    return resp.text

def fetch_summary_text(url: str) -> str:
    """
    Returns a cleaned text for NLP.
    - Normal path: requests
    - RBNZ fallback: pyppeteer using *local* Chrome/Edge only (no Chromium download).
                     If no local browser -> skip fallback and return "".
    """
    html = ""
    try:
        html = _requests_get_html(url)
    except Exception:
        if _is_rbnz(url) and HAS_PYPPETEER:
            try:
                loop = None
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    html = new_loop.run_until_complete(_fetch_html_with_pyppeteer(url))
                    new_loop.close()
                    asyncio.set_event_loop(loop)
                else:
                    loop = loop or asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    html = loop.run_until_complete(_fetch_html_with_pyppeteer(url))
                    loop.close()
            except Exception:
                # gracefully skip (avoid download loops)
                html = ""
        else:
            # not RBNZ (or no pyppeteer) -> skip
            html = ""

    text = _extract_visible_text(html)
    return _clean_text(text)
