# file: cb_sources.py
from __future__ import annotations

import json, re, time
import requests, xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Dict, List, Protocol, Tuple, Callable, Any
from email.utils import parsedate_to_datetime
from datetime import timezone, timedelta, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

# HTML parsing
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

# JS rendering (optionnel)
try:
    from requests_html import HTMLSession
    HAS_REQUESTS_HTML = True
except Exception:
    HAS_REQUESTS_HTML = False


# ---------- Interface commune ----------
class CentralBankSource(Protocol):
    id: str
    name: str
    def fetch_latest_meta(self) -> Optional[Dict[str, str]]: ...
    def fetch_recent_meta(self, n: int = 5) -> List[Dict[str, str]]: ...

# ---------- Outils communs ----------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    )
}

# Mois (FR/EN) pour parsing libre
MONTHS = {
    "jan": 1, "january": 1, "janvier": 1,
    "feb": 2, "february": 2, "février": 2, "fevrier": 2,
    "mar": 3, "march": 3, "mars": 3,
    "apr": 4, "april": 4, "avril": 4,
    "may": 5, "mai": 5,
    "jun": 6, "june": 6, "juin": 6,
    "jul": 7, "july": 7, "juillet": 7,
    "aug": 8, "august": 8, "août": 8, "aout": 8,
    "sep": 9, "september": 9, "septembre": 9,
    "oct": 10, "october": 10, "octobre": 10,
    "nov": 11, "november": 11, "novembre": 11,
    "dec": 12, "december": 12, "décembre": 12, "decembre": 12,
}

_PARIS = ZoneInfo("Europe/Paris")

def _format_dt_paris(dt: datetime) -> str:
    dtp = dt.astimezone(_PARIS)
    offset = dtp.utcoffset() or timedelta(0)
    hours = int(offset.total_seconds() // 3600)
    sign = "+" if hours >= 0 else "-"
    hours_abs = abs(hours)
    hour12 = dtp.strftime("%I").lstrip("0") or "12"
    minute = dtp.strftime("%M")
    ampm = dtp.strftime("%p")
    return f"{dtp.date().isoformat()} {hour12}:{minute}{ampm} GMT{sign}{hours_abs}"

def _format_pubdate_rss(pubdate_raw: str) -> str:
    try:
        dt = parsedate_to_datetime(pubdate_raw.strip())
        return _format_dt_paris(dt)
    except Exception:
        return pubdate_raw.strip()

def _format_pubdate_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return _format_dt_paris(dt)

def _normalize_url(url: str) -> str:
    u = url
    # corrige l’URL BoE où les query params peuvent être collés
    if "?" not in u and ("NewsTypes=" in u or "Taxonomies=" in u or "Direction=" in u):
        u = u.replace("newsNewsTypes=", "news?NewsTypes=")
        if "NewsTypes=" in u and "?" not in u:
            u = u.replace("NewsTypes=", "?NewsTypes=")
        if "Taxonomies=" in u and "?" not in u:
            u = u.replace("Taxonomies=", "?Taxonomies=")
        if "Direction=" in u and "?" not in u:
            u = u.replace("Direction=", "?Direction=")
    return u

def _get_url(url: str, *, force_refresh: bool = True) -> requests.Response:
    base = _normalize_url(url)
    # Pas de cache-buster pour les flux XML
    if base.lower().endswith((".xml", ".rss", ".atom")):
        q = base
    else:
        if force_refresh:
            if "#" in base:
                base, frag = base.split("#", 1)
                frag = "#" + frag
            else:
                frag = ""
            sep = "&" if "?" in base else "?"
            q = f"{base}{sep}t={int(time.time())}{frag}"
        else:
            q = base
    resp = requests.get(q, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp

def _fetch_html_dynamic(url: str, wait: float = 1.5, timeout: int = 30) -> Optional[str]:
    """
    Récupère le HTML *après exécution JS* avec requests_html (si dispo).
    Si Chromium n'est pas encore installé (1er run), on renvoie None proprement.
    """
    if not HAS_REQUESTS_HTML:
        return None
    session = None
    try:
        from pyppeteer.chromium_downloader import check_chromium
        # Si Chromium n'est pas encore installé, on ne tente pas de render
        try:
            check_chromium()
        except Exception:
            return None

        session = HTMLSession()
        r = session.get(url, headers=HEADERS, timeout=timeout)
        # Important: un petit temps pour laisser charger les scripts
        r.html.render(sleep=wait, timeout=timeout, retries=1)
        return r.html.html
    except Exception:
        return None
    finally:
        try:
            if session:
                session.close()
        except Exception:
            pass


def _looks_like_xml(resp: requests.Response) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "xml" in ctype:
        return True
    start = resp.content[:200].lstrip()
    return start.startswith(b"<?xml") or b"<rss" in start or b"<feed" in start

# ---------- Parsing RSS (ordre du flux) ----------
def _parse_rss_items(resp: requests.Response) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return []
    channel = root.find("channel")
    if channel is None:
        return []
    items = channel.findall("item") or []
    out: List[Dict[str, str]] = []
    for it in items:
        title = (it.findtext("title") or "").strip() or None
        link  = (it.findtext("link")  or "").strip() or None
        pub   = (it.findtext("pubDate") or "").strip()
        pub_f = _format_pubdate_rss(pub) if pub else None
        if link:
            out.append({"title": title, "link": link, "pubDate": pub_f})
    return out

# ---------- Aides parsing HTML ----------
DATE_ATTRS = ["datetime", "data-datetime", "content", "aria-label", "title"]
DATE_PATTERNS = [
    r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})",
    r"(?P<d>\d{1,2})\s+(?P<mon>[A-Za-z]{3,9})\.?,?\s+(?P<y>\d{4})",
    r"(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})",
    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})",
    r"(?P<mon>[A-Za-z]{3,9})\s+(?P<y>\d{4})",
    r"(?P<y>\d{4})\s+(?P<mon>[A-Za-z]{3,9})",
]

def _parse_date_guess(text: str) -> Optional[datetime]:
    s = " ".join((text or "").split())
    for pat in DATE_PATTERNS:
        m = re.search(pat, s, re.IGNORECASE)
        if not m:
            continue
        gd = m.groupdict()
        try:
            if gd.get("mon"):
                mon_name = gd["mon"].lower()
                mon = MONTHS.get(mon_name)
                if not mon:
                    continue
                d = int(gd.get("d") or 1)
                return datetime(int(gd["y"]), mon, d, tzinfo=timezone.utc)
            d = int(gd.get("d") or 1)
            return datetime(int(gd["y"]), int(gd["m"]), d, tzinfo=timezone.utc)
        except Exception:
            continue
    return None

# ---- Inférence de date depuis une URL ECB /press/ ----
_ECB_URL_DATE_PAT = re.compile(
    r"/press/(?:key|pr)/date/(?P<year>\d{4})/html/ecb\.(?:sp|pr)(?P<yymmdd>\d{6})",
    re.IGNORECASE
)

def _infer_ecb_date_from_link(link: str) -> Optional[datetime]:
    m = _ECB_URL_DATE_PAT.search(link or "")
    if not m:
        return None
    year = int(m.group("year"))
    yymmdd = m.group("yymmdd")
    yy = int(yymmdd[:2]); mm = int(yymmdd[2:4]); dd = int(yymmdd[4:6])
    try:
        return datetime(year, mm, dd, tzinfo=timezone.utc)
    except Exception:
        return None

# ---- Inférence de date BoJ depuis l'URL ----
def _infer_boj_date_from_link(link: str) -> Optional[datetime]:
    """
    Retourne un datetime(UTC) pour divers formats trouvés dans les URLs BoJ.
    """
    s = (link or "").lower()

    # 8 chiffres → yyyymmdd
    m = re.search(r"(20\d{6})", s)
    if m:
        yyyymmdd = m.group(1)
        y = int(yyyymmdd[0:4]); mm = int(yyyymmdd[4:6]); dd = int(yyyymmdd[6:8])
        try:
            return datetime(y, mm, dd, tzinfo=timezone.utc)
        except Exception:
            pass

    # motif yy[a-z]mm (ex: 'wp25e08' → 2025-08-01)
    m = re.search(r"(?P<yy>\d{2})[a-z](?P<mm>\d{2})", s)
    if m:
        yy = int(m.group("yy")); mm = int(m.group("mm"))
        if 1 <= mm <= 12:
            try:
                return datetime(2000 + yy, mm, 1, tzinfo=timezone.utc)
            except Exception:
                pass

    # 6 chiffres : tenter yymmdd, sinon yyyymm
    m = re.search(r"(\d{6})", s)
    if m:
        six = m.group(1)
        yy = int(six[0:2]); mm = int(six[2:4]); dd = int(six[4:6])
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            try:
                return datetime(2000 + yy, mm, dd, tzinfo=timezone.utc)
            except Exception:
                pass
        if six.startswith("20"):
            y = int(six[0:4]); mm2 = int(six[4:6])
            if 1 <= mm2 <= 12:
                try:
                    return datetime(y, mm2, 1, tzinfo=timezone.utc)
                except Exception:
                    pass

    # 4 chiffres → yymm (ex: 2507) avec bornes
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", s)
    if m:
        yymm = m.group(1)
        yy = int(yymm[0:2]); mm = int(yymm[2:4])
        if 1 <= mm <= 12:
            try:
                return datetime(2000 + yy, mm, 1, tzinfo=timezone.utc)
            except Exception:
                pass

    return None

# ---------- Extracteur JSON-LD ----------
def _extract_items_jsonld(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "lxml")
    scripts = soup.find_all("script", type="application/ld+json")
    items: List[Tuple[Optional[datetime], str, str]] = []

    def _coerce_list(x: Any) -> List[Any]:
        if x is None:
            return []
        return x if isinstance(x, list) else [x]

    def _to_dt(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            if re.match(r"^\d{4}-\d{2}-\d{2}", s):
                y, m, d = map(int, s[:10].split("-"))
                return datetime(y, m, d, tzinfo=timezone.utc)
        except Exception:
            pass
        return _parse_date_guess(s)

    def _harvest(obj: Any):
        if isinstance(obj, dict):
            if "@graph" in obj:
                for node in _coerce_list(obj["@graph"]):
                    _harvest(node)
                return
            t = obj.get("@type") or obj.get("type")
            if isinstance(t, list):
                t = t[0] if t else None
            t = (t or "").lower()
            if t in {"newsarticle","article","blogposting","pressrelease","creativework","report","publicationevent"}:
                title = (obj.get("headline") or obj.get("name") or "").strip()
                link  = (obj.get("url") or "").strip()
                date  = obj.get("datePublished") or obj.get("dateCreated") or obj.get("startDate") or obj.get("endDate")
                if title and link:
                    if not link.startswith("http"):
                        link = urljoin(url, link)
                    items.append((_to_dt(date), title, link))
            for _, v in obj.items():
                _harvest(v)
        elif isinstance(obj, list):
            for x in obj:
                _harvest(x)

    for s in scripts:
        try:
            data = json.loads(s.string or "{}")
            _harvest(data)
        except Exception:
            continue

    seen = set()
    uniq: List[Tuple[Optional[datetime], str, str]] = []
    for dt, title, link in items:
        if link in seen:
            continue
        seen.add(link)
        uniq.append((dt, title, link))
        if len(uniq) >= max_items:
            break
    return uniq

# ---------- Extracteur HTML générique ----------
def _extract_items_from_html(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "lxml")

    dated: List[Tuple[Optional[datetime], str, str]] = []
    undated: List[Tuple[Optional[datetime], str, str]] = []

    def _push(dt, title, link):
        (dated if dt else undated).append((dt, title, link))

    for art in soup.select("article"):
        a = art.find("a", href=True)
        if not a or not a.get_text(strip=True):
            continue
        title = a.get_text(" ", strip=True)
        link = urljoin(url, a["href"])
        dt = None
        t = art.find("time")
        if t:
            for attr in DATE_ATTRS:
                val = t.get(attr)
                if val:
                    dt = _parse_date_guess(val) or dt
            if not dt:
                dt = _parse_date_guess(t.get_text(" ", strip=True))
        if not dt:
            dt = _parse_date_guess(art.get_text(" ", strip=True))
        _push(dt, title, link)
        if len(dated) + len(undated) >= max_items:
            break

    if len(dated) + len(undated) < max_items:
        for li in soup.select("li"):
            a = li.find("a", href=True)
            if not a or not a.get_text(strip=True):
                continue
            title = a.get_text(" ", strip=True)
            link = urljoin(url, a["href"])
            dt = None
            t = li.find("time")
            if t:
                for attr in DATE_ATTRS:
                    val = t.get(attr)
                    if val:
                        dt = _parse_date_guess(val) or dt
                if not dt:
                    dt = _parse_date_guess(t.get_text(" ", strip=True))
            if not dt:
                dt = _parse_date_guess(li.get_text(" ", strip=True))
            _push(dt, title, link)
            if len(dated) + len(undated) >= max_items:
                break

    if len(dated) + len(undated) < max_items:
        for a in soup.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            link = urljoin(url, a["href"])
            if any(x in link.lower() for x in ["/rss", "/feed", "#", "/search", "/terms", "/privacy"]):
                continue
            dt = _parse_date_guess(a.get_text(" ", strip=True))
            _push(dt, title, link)
            if len(dated) + len(undated) >= max_items:
                break

    seen = set()
    out: List[Tuple[Optional[datetime], str, str]] = []
    for bucket in (dated, undated):
        for dt, title, link in bucket:
            if link in seen:
                continue
            seen.add(link)
            out.append((dt, title, link))
            if len(out) >= max_items:
                return out
    return out

# ---------- Extracteur spécifique: ECB press ----------
def _extract_items_ecb_press(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    jl = _extract_items_jsonld(url, html, max_items=max_items)
    jl_press = [(dt, t, l) for (dt, t, l) in jl if "/press/" in (l or "")]
    if jl_press:
        return jl_press[:max_items]

    if HAS_BS4:
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("main") or soup.find(id="content") or soup
        anchors = root.find_all("a", href=True)
        items: List[Tuple[Optional[datetime], str, str]] = []
        for a in anchors:
            href = a.get("href") or ""
            if "/press/" not in href:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            link = urljoin(url, href)

            dt = _infer_ecb_date_from_link(link) or None
            par = a.parent
            for node in (par, getattr(par, "parent", None)):
                if not node or dt is not None:
                    continue
                t = node.find("time")
                if t:
                    for attr in DATE_ATTRS:
                        val = t.get(attr)
                        if val:
                            dt = _parse_date_guess(val) or dt
                    if dt is None:
                        dt = _parse_date_guess(t.get_text(" ", strip=True))
                if dt is None:
                    dt = _parse_date_guess(node.get_text(" ", strip=True))

            items.append((dt, title, link))
            if len(items) >= max_items:
                break
        if items:
            seen = set()
            uniq: List[Tuple[Optional[datetime], str, str]] = []
            for dt, t, l in items:
                if l in seen:
                    continue
                seen.add(l)
                uniq.append((dt, t, l))
                if len(uniq) >= max_items:
                    break
            return uniq

    try:
        resp = _get_url("https://www.ecb.europa.eu/rss/press.html", force_refresh=False)
        rss_items = _parse_rss_items(resp)
        out: List[Tuple[Optional[datetime], str, str]] = []
        for it in rss_items[:max_items]:
            l = it.get("link") or ""
            t = it.get("title") or ""
            dt = _infer_ecb_date_from_link(l)
            out.append((dt, t, l))
        return out
    except Exception:
        return []

# ---------- Extracteur spécifique: BoE news -> RSS ----------
def _extract_items_boe_news(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    if "/news" in url:
        if "?" in url:
            base, query = url.split("?", 1)
            rss_url = base.replace("/news", "/rss/news") + "?" + query
        else:
            rss_url = url.replace("/news", "/rss/news")
    else:
        rss_url = url

    try:
        resp = _get_url(rss_url, force_refresh=False)
        if not _looks_like_xml(resp):
            return []

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        out: List[Tuple[Optional[datetime], str, str]] = []
        DC_DATE_TAG = "{http://purl.org/dc/elements/1.1/}date"

        for it in channel.findall("item")[:max_items]:
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link")  or "").strip()
            pub_rfc = (it.findtext("pubDate") or "").strip()
            pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()

            dt: Optional[datetime] = None
            if pub_rfc:
                try:
                    dt = parsedate_to_datetime(pub_rfc).astimezone(timezone.utc)
                except Exception:
                    dt = None
            if dt is None and pub_dc:
                try:
                    iso = pub_dc.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(iso)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                except Exception:
                    dt = None

            if title and link:
                out.append((dt, title, link))

        return out
    except Exception:
        return []

# ---------- Extracteur spécifique : news FED (fusion 2 flux) ----------
def _extract_items_fed_latest(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    feeds = [
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ]
    DC_DATE_TAG = "{http://purl.org/dc/elements/1.1/}date"

    def parse_feed(rss_url: str) -> List[Tuple[Optional[datetime], str, str]]:
        try:
            resp = _get_url(rss_url, force_refresh=False)
            if not _looks_like_xml(resp):
                return []
            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            if channel is None:
                return []
            rows: List[Tuple[Optional[datetime], str, str]] = []
            for it in channel.findall("item"):
                title = (it.findtext("title") or "").strip()
                link  = (it.findtext("link")  or "").strip()
                pub_rfc = (it.findtext("pubDate") or "").strip()
                pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()

                dt: Optional[datetime] = None
                if pub_rfc:
                    try:
                        dt = parsedate_to_datetime(pub_rfc).astimezone(timezone.utc)
                    except Exception:
                        dt = None
                if dt is None and pub_dc:
                    try:
                        iso = pub_dc.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(iso)
                        dt = (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc))
                    except Exception:
                        dt = None

                if title and link:
                    rows.append((dt, title, link))
            return rows
        except Exception:
            return []

    all_rows: List[Tuple[Optional[datetime], str, str]] = []
    for f in feeds:
        all_rows.extend(parse_feed(f))

    # dédup par lien en gardant la date la plus récente
    by_link: Dict[str, Tuple[Optional[datetime], str, str]] = {}
    for dt, title, link in all_rows:
        if not link:
            continue
        cur = by_link.get(link)
        if (cur is None) or (dt and (cur[0] is None or dt > cur[0])):
            by_link[link] = (dt, title, link)

    out = list(by_link.values())
    out.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
    return out[:max_items]

# ---------- Extracteur spécifique : news BoJ (Atom/RSS/RDF) ----------
def _extract_items_boj_rss(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    """
    Bank of Japan — What's New (EN)
    1) Flux XML officiel (Atom/RSS/RDF)
    2) Fallback HTML : parcourt les listes et infère la date via _parse_date_guess
    """
    rss_url = "https://www.boj.or.jp/en/whatsnew/feeds/whatsnew_e.xml"
    DEBUG = False  # passe à True si tu veux voir les entêtes/200 octets

    def _to_dt_rfc822(s: str) -> Optional[datetime]:
        try:
            return parsedate_to_datetime(s.strip()).astimezone(timezone.utc)
        except Exception:
            return None

    def _to_dt_iso(s: str) -> Optional[datetime]:
        if not s:
            return None
        s = s.strip()
        try:
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            m = re.match(r"^(\d{4})[-./](\d{2})[-./](\d{2})", s)
            if m:
                y, mth, d = map(int, m.groups())
                return datetime(y, mth, d, tzinfo=timezone.utc)
            return None

    def _parse_xml(resp: requests.Response) -> List[Tuple[Optional[datetime], str, str]]:
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        out: List[Tuple[Optional[datetime], str, str]] = []
        DC_DATE_TAG = "{http://purl.org/dc/elements/1.1/}date"

        # RSS 2.0
        channel = root.find("channel")
        if channel is not None:
            for it in channel.findall("item")[:max_items]:
                title = (it.findtext("title") or "").strip()
                link  = ((it.findtext("link") or "") or (it.findtext("guid") or "")).strip()
                pub_rfc = (it.findtext("pubDate") or "").strip()
                pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()
                dt = _to_dt_rfc822(pub_rfc) or _to_dt_iso(pub_dc)
                if title and link:
                    out.append((dt, title, link))
            return out

        # Atom
        if root.tag.lower().endswith("feed"):
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns) or root.findall("entry")
            for entry in entries[:max_items]:
                title = (entry.findtext("atom:title", default="", namespaces=ns) or entry.findtext("title") or "").strip()
                link_el = (entry.find("atom:link[@rel='alternate']", ns) or
                           entry.find("atom:link", ns) or entry.find("link"))
                link = (link_el.get("href") if link_el is not None else None)
                pub = (entry.findtext("atom:updated", namespaces=ns) or
                       entry.findtext("atom:published", namespaces=ns) or
                       entry.findtext("updated") or
                       entry.findtext("published") or "")
                dt = _to_dt_iso(pub)
                if title and link:
                    out.append((dt, title, link))
            return out

        # RDF
        if root.tag.lower().endswith("rdf"):
            for it in root.findall("item"):
                title = (it.findtext("title") or "").strip()
                link  = (it.findtext("link")  or "").strip()
                pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()
                dt = _to_dt_iso(pub_dc)
                if title and link:
                    out.append((dt, title, link))
            return out

        return out

    # 1) Flux XML (sans cache-buster)
    try:
        resp = _get_url(rss_url, force_refresh=False)
        if DEBUG:
            try:
                print("BoJ RSS status:", resp.status_code, "| CT:", resp.headers.get("Content-Type", ""))
                print(resp.content[:200])
            except Exception:
                pass
        if _looks_like_xml(resp):
            items = _parse_xml(resp)
            items.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
            if items:
                return items[:max_items]
    except Exception:
        pass

    # 2) Fallback HTML : parcourir les listes et n'accepter que les items avec date parsée
    try:
        page_url = "https://www.boj.or.jp/en/whatsnew/index.htm"
        page = _get_url(page_url, force_refresh=True)
        if not HAS_BS4:
            return []
        soup = BeautifulSoup(page.text, "lxml")

        # zone principale
        root = soup.find("main") or soup.find(id="contents") or soup

        nav_phrases = {
            "skip to main content", "home", "日本語", "about the bank", "outline of the bank",
            "contact", "sitemap", "search"
        }

        def is_nav(title: str, href: str) -> bool:
            t = (title or "").strip().lower()
            h = (href or "").strip().lower()
            if not t or t in nav_phrases:
                return True
            if h in {"#", "/", "/en/"}:
                return True
            if h.endswith("index.htm") and "#contents" in h:
                return True
            # liens hors domaine BoJ : rarement des actus
            if not h.startswith("http") and not h.startswith("/"):
                return True
            return False

        out: List[Tuple[Optional[datetime], str, str]] = []

        # Cible prioritaire: éléments listés
        candidates = root.select("ul li, ol li, dl dd")
        if not candidates:
            candidates = root.find_all(True)

        for node in candidates:
            a = node.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            href = a["href"]
            if is_nav(title, href):
                continue

            # Cherche une date dans <time> ou le texte du bloc
            dt = None
            ttag = node.find("time")
            if ttag:
                for attr in ("datetime", "data-datetime", "title", "aria-label", "content"):
                    val = ttag.get(attr)
                    if val:
                        dt = _parse_date_guess(val)
                        if dt:
                            break
                if not dt:
                    dt = _parse_date_guess(ttag.get_text(" ", strip=True))
            if not dt:
                dt = _parse_date_guess(node.get_text(" ", strip=True))
            if not dt:
                dt = _parse_date_guess(title)
            if not dt:
                # dernier recours : inférer via l’URL (yymmdd/yyyymmdd/…)
                dt = _infer_boj_date_from_link(href)

            if not dt:
                # si aucune date trouvée, on ignore (évite la nav)
                continue

            link = urljoin(page_url, href)
            out.append((dt, title, link))
            if len(out) >= max_items:
                break

        # dédup + tri
        seen = set()
        uniq: List[Tuple[Optional[datetime], str, str]] = []
        for dt, t, l in out:
            if l in seen:
                continue
            seen.add(l)
            uniq.append((dt, t, l))
        uniq.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
        return uniq[:max_items]
    except Exception:
        return []

# ---------- Extracteur spécifique : Bank of Canada (Publications) ----------
def _extract_items_boc(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    """
    Bank of Canada — Publications browse
    1) Tente des flux RSS/Atom découverts via <link rel="alternate" ...> (si présents)
    2) Fallback HTML : ne garde que des cartes d’articles/publications, exclut pagination/recherche.
       Tente date via <time>, attributs (datetime, content, title…), classes (.date, .posted-on…), ou texte.
    Retourne [(dt_utc|None, title, link)] trié par date desc (None à la fin).
    """
    # --- helpers internes
    def _to_dt_rfc822(s: str) -> Optional[datetime]:
        try:
            return parsedate_to_datetime(s.strip()).astimezone(timezone.utc)
        except Exception:
            return None

    def _to_dt_iso(s: str) -> Optional[datetime]:
        if not s:
            return None
        s = s.strip()
        try:
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
            if m:
                y, mo, d = map(int, m.groups())
                return datetime(y, mo, d, tzinfo=timezone.utc)
            return None

    def _parse_rss_like(resp: requests.Response) -> List[Tuple[Optional[datetime], str, str]]:
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        out: List[Tuple[Optional[datetime], str, str]] = []
        DC_DATE_TAG = "{http://purl.org/dc/elements/1.1/}date"

        channel = root.find("channel")
        if channel is not None:  # RSS 2.0
            for it in channel.findall("item"):
                title = (it.findtext("title") or "").strip()
                link  = ((it.findtext("link") or "") or (it.findtext("guid") or "")).strip()
                pub_rfc = (it.findtext("pubDate") or "").strip()
                pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()
                dt = _to_dt_rfc822(pub_rfc) or _to_dt_iso(pub_dc)
                if title and link:
                    out.append((dt, title, link))
            return out

        # Atom
        if root.tag.lower().endswith("feed"):
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns) or root.findall("entry")
            for entry in entries:
                title = (entry.findtext("atom:title", default="", namespaces=ns) or entry.findtext("title") or "").strip()
                link_el = (entry.find("atom:link[@rel='alternate']", ns) or entry.find("atom:link", ns) or entry.find("link"))
                link = (link_el.get("href") if link_el is not None else None)
                pub = (entry.findtext("atom:updated", namespaces=ns) or entry.findtext("atom:published", namespaces=ns)
                       or entry.findtext("updated") or entry.findtext("published") or "")
                dt = _to_dt_iso(pub)
                if title and link:
                    out.append((dt, title, link))
            return out

        # RDF
        if root.tag.lower().endswith("rdf"):
            for it in root.findall("item"):
                title = (it.findtext("title") or "").strip()
                link  = (it.findtext("link")  or "").strip()
                pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()
                dt = _to_dt_iso(pub_dc)
                if title and link:
                    out.append((dt, title, link))
            return out

        return out

    # --- 1) tenter des flux auto-découverts (si la page les expose)
    items_rss: List[Tuple[Optional[datetime], str, str]] = []
    try:
        if HAS_BS4:
            soup_head = BeautifulSoup(html, "lxml")
            rss_links: List[str] = []
            for link in soup_head.find_all("link", rel=lambda v: v and "alternate" in v):
                t = (link.get("type") or "").lower()
                if "rss" in t or "atom" in t or "xml" in t:
                    href = (link.get("href") or "").strip()
                    if href:
                        rss_links.append(urljoin(url, href))
            rss_links = list(dict.fromkeys(rss_links))[:4]  # limite
            for rss in rss_links:
                try:
                    resp = _get_url(rss, force_refresh=False)
                    if _looks_like_xml(resp):
                        items_rss.extend(_parse_rss_like(resp))
                except Exception:
                    continue
    except Exception:
        pass

    if items_rss:
        by_link: Dict[str, Tuple[Optional[datetime], str, str]] = {}
        for dt, title, link in items_rss:
            if not link:
                continue
            prev = by_link.get(link)
            if (prev is None) or (dt and (prev[0] is None or dt > prev[0])):
                by_link[link] = (dt, title, link)
        out = list(by_link.values())
        out.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
        return out[:max_items]

    # --- 2) fallback HTML
    if not HAS_BS4:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("main") or soup.find(id="content") or soup

        out: List[Tuple[Optional[datetime], str, str]] = []

        # Sélecteurs de cartes d’articles courantes sur bankofcanada.ca
        cards = []
        cards += root.select("article")
        cards += root.select(".publication, .publications, .search-result, .search-results__item, .result, .card")

        # Si rien n’a matché, fallback aux <li> riches
        if not cards:
            cards = root.select("ul li, ol li")

        def bad_link(href: str) -> bool:
            if not href:
                return True
            h = href.lower()
            if any(s in h for s in ("mt_page=", "/search/", "/?s=", "t=", "#")):
                return True
            return False

        def extract_dt_from_node(node) -> Optional[datetime]:
            # 1) <time ...>
            ttag = node.find("time")
            if ttag:
                for attr in ("datetime", "data-datetime", "content", "title", "aria-label"):
                    val = ttag.get(attr)
                    if val:
                        dt = _parse_date_guess(val) or _to_dt_iso(val)
                        if dt:
                            return dt
                dt = _parse_date_guess(ttag.get_text(" ", strip=True))
                if dt:
                    return dt
            # 2) éléments type .date
            for cls in ["date", "posted-on", "pubdate", "publication-date", "meta"]:
                el = node.find(class_=lambda c: c and cls in str(c).lower())
                if el:
                    dt = _parse_date_guess(el.get_text(" ", strip=True))
                    if dt:
                        return dt
            # 3) texte du bloc
            txt = node.get_text(" ", strip=True)
            return _parse_date_guess(txt)

        seen = set()
        for node in cards:
            a = node.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            href = a["href"]
            if not title or title.isdigit() or bad_link(href):
                continue
            link = urljoin(url, href)
            if link in seen:
                continue
            dt = extract_dt_from_node(node)
            # Si aucune date dans la carte, essaie sur le parent immédiat
            if not dt and node.parent:
                dt = extract_dt_from_node(node.parent)
            # Dernier recours : inférer via l’URL
            if not dt:
                dt = _parse_date_guess(title) or _infer_boj_date_from_link(link)  # réutilise heuristique yyyymmdd

            # Ne garde pas les liens génériques “Publications”
            if title.strip().lower() in {"publications", "browse publications"}:
                continue

            seen.add(link)
            out.append((dt, title, link))
            if len(out) >= max_items:
                break

        # Tri: dates d’abord, None à la fin
        out.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
        return out[:max_items]

    except Exception:
        return []

# ---------- Extracteur spécifique : Reserve Bank of Australia (RBA) ----------
def _extract_items_rba(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    """
    Reserve Bank of Australia — News
    Pas de RSS public fiable => on scrape le HTML.
    Récupère (date, titre, lien) depuis les blocs de news.
    """
    if not HAS_BS4:
        return []

    soup = BeautifulSoup(html, "lxml")
    root = soup.find("main") or soup

    out: List[Tuple[Optional[datetime], str, str]] = []

    def extract_dt(node) -> Optional[datetime]:
        # 1) <time datetime="...">
        ttag = node.find("time")
        if ttag:
            for attr in ("datetime", "data-datetime", "content", "title", "aria-label"):
                val = ttag.get(attr)
                if val:
                    dt = _parse_date_guess(val)
                    if dt:
                        return dt
            txt = ttag.get_text(" ", strip=True)
            dt = _parse_date_guess(txt)
            if dt:
                return dt
        # 2) rechercher une date texte dans le bloc
        txt = node.get_text(" ", strip=True)
        return _parse_date_guess(txt)

    # Chaque news est souvent dans <article>, sinon dans <li>
    cards = root.select("article") or root.select("ul li, ol li")

    seen = set()
    for node in cards:
        a = node.find("a", href=True)
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if not title or not href:
            continue
        # filtre liens génériques
        low = href.lower()
        if any(bad in low for bad in ("/about/", "/contact", "/sitemap", "/search", "#")):
            continue

        link = urljoin(url, href)
        if link in seen:
            continue

        dt = extract_dt(node)
        if not dt and node.parent:
            dt = extract_dt(node.parent)

        seen.add(link)
        out.append((dt, title, link))
        if len(out) >= max_items:
            break

    # Tri: date desc, None à la fin
    out.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
    return out[:max_items]

# ---------- Extracteur spécifique : Reserve Bank of New Zealand (RBNZ) ----------
def _extract_items_rbnz(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    """
    RBNZ — News & Events (News)
    Sans JS. Stratégie:
      A) Essayer des flux RSS/Atom probables.
      B) Scraper la page si elle contient déjà des actus côté serveur.
      C) Découvrir et parser les sitemaps (via robots.txt puis liste de secours).
    """

    # --- Helpers date ---
    def _to_dt_rfc822(s: str) -> Optional[datetime]:
        try:
            return parsedate_to_datetime(s.strip()).astimezone(timezone.utc)
        except Exception:
            return None

    def _to_dt_iso(s: str) -> Optional[datetime]:
        if not s:
            return None
        s = s.strip()
        try:
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
            if m:
                y, mo, d = map(int, m.groups())
                return datetime(y, mo, d, tzinfo=timezone.utc)
            return None

    def _dt_from_url(u: str) -> Optional[datetime]:
        s = (u or "").lower()
        m = re.search(r"/(20\d{2})/([01]?\d)/([0-3]?\d)/", s)  # yyyy/mm/dd
        if m:
            y, mo, d = map(int, m.groups())
            try:
                return datetime(y, mo, d, tzinfo=timezone.utc)
            except Exception:
                pass
        m = re.search(r"/(20\d{2})/([a-z]{3,9})/([0-3]?\d)/", s)  # yyyy/Mon/dd
        if m:
            y = int(m.group(1)); mon = MONTHS.get(m.group(2).lower()); d = int(m.group(3))
            if mon:
                try:
                    return datetime(y, mon, d, tzinfo=timezone.utc)
                except Exception:
                    pass
        return None

    def _parse_rss_like(resp: requests.Response) -> List[Tuple[Optional[datetime], str, str]]:
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        DC_DATE_TAG = "{http://purl.org/dc/elements/1.1/}date"
        out: List[Tuple[Optional[datetime], str, str]] = []

        # RSS 2.0
        channel = root.find("channel")
        if channel is not None:
            for it in channel.findall("item"):
                title = (it.findtext("title") or "").strip()
                link  = ((it.findtext("link") or "") or (it.findtext("guid") or "")).strip()
                pub_rfc = (it.findtext("pubDate") or "").strip()
                pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()
                dt = _to_dt_rfc822(pub_rfc) or _to_dt_iso(pub_dc) or _dt_from_url(link)
                if title and link:
                    out.append((dt, title, link))
            return out

        # Atom
        if root.tag.lower().endswith("feed"):
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns) or root.findall("entry"):
                title = (entry.findtext("atom:title", default="", namespaces=ns) or entry.findtext("title") or "").strip()
                link_el = (entry.find("atom:link[@rel='alternate']", ns) or entry.find("atom:link", ns) or entry.find("link"))
                link = link_el.get("href") if link_el is not None else None
                pub = (entry.findtext("atom:updated", namespaces=ns) or entry.findtext("atom:published", namespaces=ns)
                       or entry.findtext("updated") or entry.findtext("published") or "")
                dt = _to_dt_iso(pub) or _dt_from_url(link or "")
                if title and link:
                    out.append((dt, title, link))
            return out

        return []

    def _dedup_sort(rows: List[Tuple[Optional[datetime], str, str]]) -> List[Tuple[Optional[datetime], str, str]]:
        seen = set(); uniq = []
        for dt, t, l in rows:
            if not l or l in seen:
                continue
            seen.add(l); uniq.append((dt, t, l))
        uniq.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
        return uniq[:max_items]

    # ============ A) Flux RSS/Atom candidats ============
    rss_candidates = [
        # news / media / speeches : si l’un existe, on prend
        "https://www.rbnz.govt.nz/news/feeds/news.xml",
        "https://www.rbnz.govt.nz/news/feeds/media-releases.xml",
        "https://www.rbnz.govt.nz/news/feeds/speeches.xml",
        # variantes possibles
        "https://www.rbnz.govt.nz/feeds/news.xml",
        "https://www.rbnz.govt.nz/feeds/media-releases.xml",
        "https://www.rbnz.govt.nz/feeds/speeches.xml",
        "https://www.rbnz.govt.nz/rss.xml",
    ]
    for rss in rss_candidates:
        try:
            r = _get_url(rss, force_refresh=False)
            if _looks_like_xml(r):
                rows = _parse_rss_like(r)
                if rows:
                    return _dedup_sort(rows)
        except Exception:
            continue

    # ============ B) Scraping HTML (si la page contient des actus statiquement) ============
    if HAS_BS4:
        try:
            # si WebSource a passé du HTML, on l’utilise ; sinon on fetch sans cache-buster
            if not html:
                resp = _get_url(url, force_refresh=False)
                html = resp.text
            soup = BeautifulSoup(html or "", "lxml")
            root = soup.find("main") or soup.find(id="content") or soup
            rows: List[Tuple[Optional[datetime], str, str]] = []

            def extract_dt(node) -> Optional[datetime]:
                if not node: return None
                ttag = node.find("time")
                if ttag:
                    for attr in ("datetime","data-datetime","content","title","aria-label"):
                        val = ttag.get(attr)
                        if val:
                            dt = _to_dt_iso(val) or _parse_date_guess(val)
                            if dt: return dt
                    dt = _parse_date_guess(ttag.get_text(" ", strip=True))
                    if dt: return dt
                for cls in ("date","published","meta","time"):
                    el = node.find(class_=lambda c: c and cls in str(c).lower())
                    if el:
                        dt = _parse_date_guess(el.get_text(" ", strip=True))
                        if dt: return dt
                return _parse_date_guess(node.get_text(" ", strip=True))

            cards = root.select("article, .news-item, .listing__item, .search-result, .result, .card, .tile, .teaser")
            if not cards:
                cards = root.select("ul li, ol li")

            seen = set()
            for node in cards:
                a = node.find("a", href=True)
                if not a: continue
                title = a.get_text(" ", strip=True)
                href = a["href"]
                if not title or not href: continue
                link = urljoin(url, href)
                if link in seen: continue
                seen.add(link)
                dt = extract_dt(node) or extract_dt(getattr(node, "parent", None)) or _dt_from_url(link) or _parse_date_guess(title)
                # filtre léger : on garde de préférence les chemins d'actus, sinon seulement si dt existe
                low = link.lower()
                if not any(seg in low for seg in ("/news", "/news-and-events", "/speeches", "/media-releases", "/publications")) and not dt:
                    continue
                rows.append((dt, title, link))
                if len(rows) >= max_items:
                    break

            if rows:
                return _dedup_sort(rows)
        except Exception:
            pass

    # ============ C) Sitemaps (via robots.txt + liste de secours) ============
    def _fetch_text(u: str) -> Optional[str]:
        try:
            r = _get_url(u, force_refresh=False)
            return r.text
        except Exception:
            return None

    # 1) robots.txt -> récupérer toutes les lignes "Sitemap:"
    sitemaps: List[str] = []
    robots = _fetch_text("https://www.rbnz.govt.nz/robots.txt")
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                if sm:
                    sitemaps.append(sm)

    # 2) compléments probables
    sitemaps.extend([
        "https://www.rbnz.govt.nz/sitemap.xml",
        "https://www.rbnz.govt.nz/sitemap_index.xml",
        "https://www.rbnz.govt.nz/sitemap-index.xml",
        "https://www.rbnz.govt.nz/news-and-events/sitemap.xml",
        "https://www.rbnz.govt.nz/news/sitemap.xml",
        "https://www.rbnz.govt.nz/speeches/sitemap.xml",
        "https://www.rbnz.govt.nz/media-releases/sitemap.xml",
        "https://www.rbnz.govt.nz/publications/sitemap.xml",
        "https://www.rbnz.govt.nz/sitemap.xml.gz",
    ])
    # dédup
    sitemaps = list(dict.fromkeys(sitemaps))

    def _parse_sitemap_bytes(content: bytes) -> List[Tuple[Optional[datetime], str, str]]:
        rows: List[Tuple[Optional[datetime], str, str]] = []
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return rows

        # sitemap index
        if root.tag.lower().endswith("sitemapindex"):
            for sm in root.findall(".//{*}sitemap"):
                loc = (sm.findtext("{*}loc") or "").strip()
                if not loc: continue
                rows.extend(_fetch_one_sitemap(loc))
            return rows

        # urlset
        for uel in root.findall(".//{*}url"):
            loc = (uel.findtext("{*}loc") or "").strip()
            if not loc: continue
            low = loc.lower()
            if not any(seg in low for seg in ("/news", "/news-and-events", "/speeches", "/media-releases", "/publications")):
                continue
            lastmod = (uel.findtext("{*}lastmod") or "").strip()
            dt = _parse_date_guess(lastmod) or _dt_from_url(loc)
            title = loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
            rows.append((dt, title, loc))
        return rows

    def _fetch_one_sitemap(sm_url: str) -> List[Tuple[Optional[datetime], str, str]]:
        try:
            r = _get_url(sm_url, force_refresh=False)
            # .xml
            if _looks_like_xml(r):
                return _parse_sitemap_bytes(r.content)
            # .gz
            if sm_url.endswith(".gz"):
                try:
                    import gzip
                    data = gzip.decompress(r.content)
                    return _parse_sitemap_bytes(data)
                except Exception:
                    return []
        except Exception:
            return []
        return []

    all_rows: List[Tuple[Optional[datetime], str, str]] = []
    for sm in sitemaps:
        rows = _fetch_one_sitemap(sm)
        if rows:
            all_rows.extend(rows)
    if all_rows:
        return _dedup_sort(all_rows)

    # Rien trouvé
    return []

# ---------- Extracteur spécifique : Swiss National Bank (SNB) ----------
def _extract_items_snb(url: str, html: str, max_items: int = 40) -> List[Tuple[Optional[datetime], str, str]]:
    """
    SNB — News & Publications (FR/DE/EN)
    1) Essaie des flux auto-découverts (<link rel="alternate">) et quelques candidats.
    2) Fallback HTML : scrape cartes/listes, extrait (date, titre, lien).
    3) Fallback sitemaps (robots.txt + candidats).
    Filtre les liens 'rss' / 'subscribe' et garde les chemins d'actus (inclut /mmr/).
    """

    # --- Helpers date ---
    def _to_dt_rfc822(s: str) -> Optional[datetime]:
        try:
            return parsedate_to_datetime(s.strip()).astimezone(timezone.utc)
        except Exception:
            return None

    def _to_dt_iso(s: str) -> Optional[datetime]:
        if not s:
            return None
        s = s.strip()
        try:
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            # yyyy-mm-dd / yyyy/mm/dd / dd.mm.yyyy
            m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
            if m:
                y, mo, d = map(int, m.groups())
                try:
                    return datetime(y, mo, d, tzinfo=timezone.utc)
                except Exception:
                    return None
            m = re.search(r"(?<!\d)(\d{1,2})[.](\d{1,2})[.](20\d{2})(?!\d)", s)  # dd.mm.yyyy
            if m:
                d, mo, y = map(int, m.groups())
                try:
                    return datetime(y, mo, d, tzinfo=timezone.utc)
                except Exception:
                    return None
            return None

    def _dt_from_url(u: str) -> Optional[datetime]:
        s = (u or "").lower()
        # yyyymmdd
        m = re.search(r"(20\d{6})", s)
        if m:
            ymd = m.group(1)
            y, mo, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
            try: return datetime(y, mo, d, tzinfo=timezone.utc)
            except Exception: pass
        # yyyy/mm/dd
        m = re.search(r"/(20\d{2})/([01]?\d)/([0-3]?\d)", s)
        if m:
            y, mo, d = map(int, m.groups())
            try: return datetime(y, mo, d, tzinfo=timezone.utc)
            except Exception: pass
        # yyyy-mm-dd
        m = re.search(r"(20\d{2})-([01]?\d)-([0-3]?\d)", s)
        if m:
            y, mo, d = map(int, m.groups())
            try: return datetime(y, mo, d, tzinfo=timezone.utc)
            except Exception: pass
        # dd.mm.yyyy
        m = re.search(r"(?<!\d)(\d{1,2})[.](\d{1,2})[.](20\d{2})(?!\d)", s)
        if m:
            d, mo, y = map(int, m.groups())
            try: return datetime(y, mo, d, tzinfo=timezone.utc)
            except Exception: pass
        return None

    def _parse_rss_like(resp: requests.Response) -> List[Tuple[Optional[datetime], str, str]]:
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        DC_DATE_TAG = "{http://purl.org/dc/elements/1.1/}date"
        out: List[Tuple[Optional[datetime], str, str]] = []

        # RSS 2.0
        channel = root.find("channel")
        if channel is not None:
            for it in channel.findall("item"):
                title = (it.findtext("title") or "").strip()
                link  = ((it.findtext("link") or "") or (it.findtext("guid") or "")).strip()
                pub_rfc = (it.findtext("pubDate") or "").strip()
                pub_dc  = (it.findtext(DC_DATE_TAG) or "").strip()
                dt = _to_dt_rfc822(pub_rfc) or _to_dt_iso(pub_dc) or _dt_from_url(link)
                if title and link:
                    out.append((dt, title, link))
            return out

        # Atom
        if root.tag.lower().endswith("feed"):
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns) or root.findall("entry")
            for entry in entries:
                title = (entry.findtext("atom:title", default="", namespaces=ns) or entry.findtext("title") or "").strip()
                link_el = (entry.find("atom:link[@rel='alternate']", ns) or entry.find("atom:link", ns) or entry.find("link"))
                link = link_el.get("href") if link_el is not None else None
                pub = (entry.findtext("atom:updated", namespaces=ns) or entry.findtext("atom:published", namespaces=ns)
                       or entry.findtext("updated") or entry.findtext("published") or "")
                dt = _to_dt_iso(pub) or _dt_from_url(link or "")
                if title and link:
                    out.append((dt, title, link))
            return out
        return []

    def _dedup_sort(rows: List[Tuple[Optional[datetime], str, str]]) -> List[Tuple[Optional[datetime], str, str]]:
        seen = set(); uniq = []
        for dt, t, l in rows:
            if not l or l in seen:
                continue
            # blacklist RSS / subscribe
            lo_t = (t or "").lower(); lo_l = (l or "").lower()
            if "rss" in lo_t or "rss" in lo_l or "subscribe" in lo_t or "s'abonner" in lo_t or "abonner" in lo_t:
                continue
            uniq.append((dt, t, l)); seen.add(l)
        uniq.sort(key=lambda x: (x[0] is None, x[0]), reverse=True)
        return uniq[:max_items]

    # ============ 1) Flux auto-découverts + candidats ============
    rss_rows: List[Tuple[Optional[datetime], str, str]] = []
    try:
        if not html:
            resp0 = _get_url(url, force_refresh=False)
            html = resp0.text
        if HAS_BS4 and html:
            soup_head = BeautifulSoup(html, "lxml")
            rss_links: List[str] = []
            for link in soup_head.find_all("link", rel=lambda v: v and "alternate" in v):
                t = (link.get("type") or "").lower()
                if "rss" in t or "atom" in t or "xml" in t:
                    href = (link.get("href") or "").strip()
                    if href:
                        rss_links.append(urljoin(url, href))
            # ajouter quelques candidats probables
            rss_links += [
                "https://www.snb.ch/fr/news-publications/news/rss",   # si existe
                "https://www.snb.ch/en/news-and-publications/news/rss",
                "https://www.snb.ch/de/aktuell-und-publikationen/news/rss",
            ]
            rss_links = list(dict.fromkeys(rss_links))[:8]
            for rss in rss_links:
                try:
                    r = _get_url(rss, force_refresh=False)
                    if _looks_like_xml(r):
                        rss_rows.extend(_parse_rss_like(r))
                        continue
                    # essais rapides si la page rss n'est pas directement XML
                    for guess in (rss + ".xml", rss.rstrip("/") + "/rss.xml"):
                        try:
                            r2 = _get_url(guess, force_refresh=False)
                            if _looks_like_xml(r2):
                                rss_rows.extend(_parse_rss_like(r2))
                                break
                        except Exception:
                            continue
                except Exception:
                    continue
    except Exception:
        pass

    if rss_rows:
        return _dedup_sort(rss_rows)

    # ============ 2) Fallback HTML scraping ============
    if HAS_BS4 and html:
        try:
            soup = BeautifulSoup(html, "lxml")
            root = soup.find("main") or soup.find(id="content") or soup
            rows_html: List[Tuple[Optional[datetime], str, str]] = []

            def extract_dt(node) -> Optional[datetime]:
                if not node: return None
                ttag = node.find("time")
                if ttag:
                    for attr in ("datetime", "data-datetime", "content", "title", "aria-label"):
                        val = ttag.get(attr)
                        if val:
                            dt = _to_dt_iso(val) or _parse_date_guess(val)
                            if dt: return dt
                    dt = _parse_date_guess(ttag.get_text(" ", strip=True))
                    if dt: return dt
                # classes multi-langues
                for cls in ("date","datum","publie","publié","published","meta","time"):
                    el = node.find(class_=lambda c: c and cls in str(c).lower())
                    if el:
                        dt = _parse_date_guess(el.get_text(" ", strip=True)) or _to_dt_iso(el.get_text(" ", strip=True))
                        if dt: return dt
                # texte global (+ dd.mm.yyyy)
                txt = node.get_text(" ", strip=True)
                m = re.search(r"(?<!\d)(\d{1,2})[.](\d{1,2})[.](20\d{2})(?!\d)", txt)
                if m:
                    d, mo, y = map(int, m.groups())
                    try: return datetime(y, mo, d, tzinfo=timezone.utc)
                    except Exception: pass
                return _parse_date_guess(txt) or _to_dt_iso(txt)

            def bad_link(href: str, title: str) -> bool:
                if not href: return True
                lo_h = href.lower(); lo_t = (title or "").lower()
                if ("rss" in lo_h or "rss" in lo_t or "subscribe" in lo_t or "s'abonner" in lo_t or "abonner" in lo_t):
                    return True
                return False

            cards = root.select("article, .news-item, .listing__item, .search-result, .result, .card, .tile, .teaser")
            if not cards:
                cards = root.select("ul li, ol li")

            seen = set()
            for node in cards:
                a = node.find("a", href=True)
                if not a: continue
                title = a.get_text(" ", strip=True)
                href = a["href"]
                if not title or not href or bad_link(href, title): continue
                link = urljoin(url, href)
                if link in seen: continue
                seen.add(link)

                dt = extract_dt(node) or extract_dt(getattr(node, "parent", None)) or _dt_from_url(link) or _parse_date_guess(title)
                # garder prioritairement les chemins d'actus/publications + /mmr/
                low = link.lower()
                if not any(seg in low for seg in (
                    "/news", "/news-publications", "/publications",
                    "/communiques", "/medienmitteilungen", "/nouvelles", "/mmr/"
                )) and not dt:
                    continue

                rows_html.append((dt, title, link))
                if len(rows_html) >= max_items:
                    break

            if rows_html:
                return _dedup_sort(rows_html)
        except Exception:
            pass

    # ============ 3) Fallback sitemaps ============
    def _fetch_text(u: str) -> Optional[str]:
        try:
            r = _get_url(u, force_refresh=False)
            return r.text
        except Exception:
            return None

    sitemaps: List[str] = []
    robots = _fetch_text("https://www.snb.ch/robots.txt")
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                if sm:
                    sitemaps.append(sm)

    sitemaps.extend([
        "https://www.snb.ch/sitemap.xml",
        "https://www.snb.ch/sitemap_index.xml",
        "https://www.snb.ch/sitemap-index.xml",
        "https://www.snb.ch/fr/sitemap.xml",
        "https://www.snb.ch/en/sitemap.xml",
        "https://www.snb.ch/de/sitemap.xml",
        "https://www.snb.ch/fr/news-publications/sitemap.xml",
        "https://www.snb.ch/en/news-and-publications/sitemap.xml",
        "https://www.snb.ch/de/aktuell-und-publikationen/sitemap.xml",
    ])
    sitemaps = list(dict.fromkeys(sitemaps))

    def _parse_sitemap_bytes(content: bytes) -> List[Tuple[Optional[datetime], str, str]]:
        rows: List[Tuple[Optional[datetime], str, str]] = []
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return rows

        if root.tag.lower().endswith("sitemapindex"):
            for sm in root.findall(".//{*}sitemap"):
                loc = (sm.findtext("{*}loc") or "").strip()
                if not loc: continue
                rows.extend(_fetch_one_sitemap(loc))
            return rows

        for uel in root.findall(".//{*}url"):
            loc = (uel.findtext("{*}loc") or "").strip()
            if not loc: continue
            low = loc.lower()
            # inclure /mmr/ (media) et éviter rss
            if ("rss" in low) or not any(seg in low for seg in (
                "/news", "/news-publications", "/publications",
                "/communiques", "/medienmitteilungen", "/nouvelles", "/mmr/"
            )):
                continue
            lastmod = (uel.findtext("{*}lastmod") or "").strip()
            dt = _parse_date_guess(lastmod) or _to_dt_iso(lastmod) or _dt_from_url(loc)
            title = loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
            # blacklist titre rss
            if "rss" in title.lower():
                continue
            rows.append((dt, title, loc))
        return rows

    def _fetch_one_sitemap(sm_url: str) -> List[Tuple[Optional[datetime], str, str]]:
        try:
            r = _get_url(sm_url, force_refresh=False)
            if _looks_like_xml(r):
                return _parse_sitemap_bytes(r.content)
            if sm_url.endswith(".gz"):
                try:
                    import gzip
                    data = gzip.decompress(r.content)
                    return _parse_sitemap_bytes(data)
                except Exception:
                    return []
        except Exception:
            return []
        return []

    all_rows: List[Tuple[Optional[datetime], str, str]] = []
    for sm in sitemaps:
        rows = _fetch_one_sitemap(sm)
        if rows:
            all_rows.extend(rows)
    if all_rows:
        return _dedup_sort(all_rows)

    return []


# ---------- Source auto ----------
@dataclass
class WebSource:
    id: str
    name: str
    url: str  # RSS XML ou page HTML
    extractor: Optional[Callable[[str, str, int], List[Tuple[Optional[datetime], str, str]]]] = None

    def _fetch_items(self) -> List[Dict[str, str]]:
        # Cas 1 : pas d'extracteur -> on s'attend à du XML direct (RSS/Atom)
        if self.extractor is None:
            resp = _get_url(self.url, force_refresh=True)
            if _looks_like_xml(resp):
                return _parse_rss_items(resp)
            return []

        # Cas 2 : extracteur spécifique
        if self.id.lower() == "rbnz":
            #  RBNZ refuse le cache-buster -> l'extracteur refetch SANS cache-buster
            try:
                items3 = self.extractor(self.url, "", 40) or []
            except Exception:
                items3 = []
        else:
            # Pré-fetch normal (avec cache-buster) pour les autres banques
            resp = _get_url(self.url, force_refresh=True)
            if _looks_like_xml(resp):
                # Si l’URL pointe finalement vers un flux XML
                return _parse_rss_items(resp)
            items3 = self.extractor(self.url, resp.text, 40) or []

        # Normalisation des items
        items: List[Dict[str, str]] = []
        for dt, title, link in items3:
            pub = _format_pubdate_iso(dt) if dt else None
            items.append({"title": title or None, "link": link, "pubDate": pub})
        return items

    def fetch_latest_meta(self) -> Optional[Dict[str, str]]:
        items = self._fetch_items()
        return items[0] if items else None

    def fetch_recent_meta(self, n: int = 5) -> List[Dict[str, str]]:
        items = self._fetch_items()
        return items[:n]


# ---------- Déclarations ----------
BOE = WebSource(
    id="boe",
    name="Bank of England",
    url=(
        "https://www.bankofengland.co.uk/news?"
        "NewsTypes=ce90163e489841e0b66d06243d35d5cb&"
        "Taxonomies=7af03071367c4a5d80cfb86f9c954759&"
        "Direction=Latest"
    ),
    extractor=_extract_items_boe_news
)

FED = WebSource(
    id="fed",
    name="Federal Reserve",
    url="https://www.federalreserve.gov/newsevents.htm",
    extractor=_extract_items_fed_latest
)

ECB = WebSource(
    id="ecb",
    name="European Central Bank",
    url="https://www.ecb.europa.eu/press/pubbydate/html/index.en.html?",
    extractor=_extract_items_ecb_press
)

BOJ = WebSource(
    id="boj",
    name="Bank of Japan",
    url="https://www.boj.or.jp/en/whatsnew/index.htm",
    extractor=_extract_items_boj_rss
)

BOC = WebSource(
    id="boc",
    name="Bank of Canada",
    url="https://www.bankofcanada.ca/publications/browse/",
    extractor=_extract_items_boc
)



RBA = WebSource(
    id="rba",
    name="Reserve Bank of Australia",
    url="https://www.rba.gov.au/news/",
    extractor=_extract_items_rba
)


RBNZ = WebSource(
    id="rbnz",
    name="Reserve Bank of New Zealand",
    url="https://www.rbnz.govt.nz/news-and-events/news",
    extractor=_extract_items_rbnz
)


SNB = WebSource(
    id="snb",
    name="Swiss National Bank",
    url="https://www.snb.ch/fr/news-publications/news",
    extractor=_extract_items_snb
)
"""
## ---------- Mode test ----------
if __name__ == "__main__":
    def _run_test(label: str, source: WebSource):
        print(f"[TEST] {label} — 5 derniers items:")
        try:
            items = source.fetch_recent_meta(n=5)
            print(f"Fetched {len(items)} items")
            for i, it in enumerate(items, 1):
                print(f"{i}. {it.get('pubDate','—')} | {it['title']} | {it['link']}")
        except Exception as e:
            print(f"Erreur test {label}:", e)
        print()  # ligne vide

    _run_test("BoJ", BOJ)
    _run_test("BoE", BOE)
    _run_test("Fed", FED)
    _run_test("ECB", ECB)
    _run_test("BoC", BOC)
    _run_test("RBA", RBA)
    _run_test("RBNZ", RBNZ)
    _run_test("SNB", SNB) 
"""
