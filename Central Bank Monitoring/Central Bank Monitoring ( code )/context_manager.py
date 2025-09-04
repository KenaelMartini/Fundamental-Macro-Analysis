# file: context_manager.py
import os, json, time
from typing import List, Dict, Optional
from parser import fetch_summary_text  # ta fonction existante

CONTEXT_DIR = os.path.join("data", "context")
os.makedirs(CONTEXT_DIR, exist_ok=True)

def _path(bank_id: str) -> str:
    return os.path.join(CONTEXT_DIR, f"{bank_id}.json")

def load_context(bank_id: str) -> List[Dict]:
    try:
        with open(_path(bank_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_context(bank_id: str, docs: List[Dict]) -> None:
    with open(_path(bank_id), "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False, indent=2)

def build_context_from_meta(bank_id: str, recent_meta: List[Dict], max_docs: int = 5) -> List[Dict]:
    """
    recent_meta: list de dicts {title, link, pubDate}
    Retourne liste [{title, date, link, text}] (max max_docs) pour cette banque.
    """
    docs = []
    for m in recent_meta[:max_docs]:
        link = (m.get("link") or "").strip()
        if not link:
            continue
        try:
            txt = fetch_summary_text(link) or ""
        except Exception:
            txt = ""
        if (txt or "").strip():
            docs.append({
                "title": m.get("title") or "—",
                "date": m.get("pubDate") or "—",
                "link": link,
                "text": txt
            })
    return docs

def get_or_refresh_context(bank_obj, max_docs: int = 5, force: bool = False, refresh_sec: int = 3600) -> List[Dict]:
    """
    Charge le contexte depuis disque; si vide/obsolète/force, reconstruit depuis le RSS.
    """
    bank_id = bank_obj.id
    path = _path(bank_id)
    # petite méta simple pour rafraîchir toutes les 'refresh_sec'
    need_refresh = force
    try:
        stat = os.stat(path)
        age = time.time() - stat.st_mtime
        if age >= refresh_sec:
            need_refresh = True
    except FileNotFoundError:
        need_refresh = True

    if not need_refresh:
        return load_context(bank_id)

    meta = []
    try:
        # ➜ nécessite RssSource.fetch_recent_meta
        meta = bank_obj.fetch_recent_meta(n=max_docs)
    except Exception:
        meta = []

    docs = build_context_from_meta(bank_id, meta, max_docs=max_docs)
    if docs:
        save_context(bank_id, docs)
        return docs

    # fallback: renvoyer ce qu'on a déjà si reconstruction échoue
    return load_context(bank_id)
