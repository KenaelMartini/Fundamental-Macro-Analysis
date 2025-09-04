import os, json, time, hashlib, argparse
from datetime import datetime, timezone

from scraper import fetch_latest_mpc_summary
from parser import fetch_summary_text
from nlp_analyzer import analyze_tone
from nlp_advanced import advanced_analyze, pretty_print_result
from mailer import send_notification

# =========================
# Arguments CLI (optionnels)
# =========================
parser = argparse.ArgumentParser()
parser.add_argument("--debug", action="store_true", help="Logs détaillés.")
args = parser.parse_args()
DEBUG = args.debug or (os.getenv("DEBUG", "false").lower() == "true")

# =========================
# Réglages (configurables via ENV)
# =========================
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SEC", "0.025"))   # 25ms
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_SEC", "60"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SEC", "60"))

# Taille max transmise à l'analyse avancée (évite 'index out of range' côté HF)
ADV_MAX_CHARS_PRIMARY   = int(os.getenv("ADV_MAX_CHARS_PRIMARY", "4000"))
ADV_MAX_CHARS_SECONDARY = int(os.getenv("ADV_MAX_CHARS_SECONDARY", "2500"))
ADV_MAX_CHARS_TERTIARY  = int(os.getenv("ADV_MAX_CHARS_TERTIARY", "1500"))

STATE_PATH = os.path.join("data", ".last_mpc.json")

# =========================
# UI (Rich si dispo)
# =========================
HAS_RICH = False
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    console = Console()
    HAS_RICH = True
except Exception:
    console = None

def ui_info(msg):  console.print(f"[bold cyan]{msg}[/]") if HAS_RICH else print(msg)
def ui_warn(msg):  console.print(f"[bold yellow]{msg}[/]") if HAS_RICH else print(msg)
def ui_error(msg): console.print(f"[bold red]{msg}[/]") if HAS_RICH else print(msg)
def ui_success(msg): console.print(f"[bold green]{msg}[/]") if HAS_RICH else print(msg)

def ui_header(state):
    text = (
        f"Watcher MPC — Poll={POLL_INTERVAL:.2f}s • Cooldown={COOLDOWN_SECONDS}s • Heartbeat={HEARTBEAT_INTERVAL}s\n"
        f"État chargé: last_link={state.get('last_link')}, last_date={state.get('last_pubdate')}, "
        f"last_hash={(state.get('last_hash') or '')[:10]}..., last_run_ts={state.get('last_run_ts')}"
    )
    if HAS_RICH:
        console.print(Panel(Text(text), title="Démarrage", border_style="cyan"))
    else:
        print("="*80); print(text); print("="*80)

def dbg(msg):
    if DEBUG:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        ui_warn(f"[DEBUG {stamp}] {msg}")

# =========================
# Persistance
# =========================
def _ensure_data_dir():
    os.makedirs("data", exist_ok=True)

def _load_state():
    _ensure_data_dir()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_link": None, "last_pubdate": None, "last_hash": None, "last_run_ts": 0.0}

def _save_state(state):
    _ensure_data_dir()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def _now_ts() -> float:
    return time.time()

# =========================
# Helpers de nettoyage / truncation
# =========================
NOISY_MARKERS = ("Nothing searched for.", "cookies", "newsletter")

def _clean_text(raw: str) -> str:
    cleaned = "\n".join(
        [ln for ln in (raw or "").splitlines() if not any(m in ln for m in NOISY_MARKERS)]
    ).strip()
    return cleaned

def _truncate(text: str, limit: int) -> str:
    t = (text or "")
    if len(t) <= limit:
        return t
    return t[:limit]

def _looks_good(adv_result: dict) -> bool:
    """
    Heuristique: on considère 'bon' si on a au moins un label ET des scores,
    ou si 'insights' ne commence pas par un message d'indispo.
    """
    if not isinstance(adv_result, dict):
        return False
    labels = adv_result.get("labels") or []
    scores = adv_result.get("scores") or []
    insights = (adv_result.get("insights") or "").strip().lower()
    if labels and scores:
        return True
    if insights and not insights.startswith("[indisponible]"):
        return True
    return False

def _adv_with_retries(text: str):
    """
    Appelle advanced_analyze en essayant plusieurs tailles (dégressives).
    Objectif: éviter les erreurs 'index out of range' côté HF et retourner un résultat 'plein'.
    """
    candidates = [
        _truncate(text, ADV_MAX_CHARS_PRIMARY),
        _truncate(text, ADV_MAX_CHARS_SECONDARY),
        _truncate(text, ADV_MAX_CHARS_TERTIARY),
    ]

    last = None
    for idx, sample in enumerate(candidates, 1):
        try:
            dbg(f"advanced_analyze try#{idx} with {len(sample)} chars")
            res = advanced_analyze(sample)
            last = res
            if _looks_good(res):
                return res
        except Exception as e:
            dbg(f"advanced_analyze error on try#{idx}: {e}")
            last = {"labels": [], "scores": [], "tone_abs_no_context": "non précisé",
                    "summary": "", "insights": f"[Indisponible] {e}",
                    "meta": {"context_window_days": 30, "model_primary": "?", "model_fallback": "?"}}
    # Si rien de "bon", on renvoie le dernier résultat (au moins structurellement valide)
    return last or {
        "labels": [], "scores": [], "tone_abs_no_context": "non précisé",
        "summary": "", "insights": "[Indisponible] Analyse non disponible",
        "meta": {"context_window_days": 30, "model_primary": "?", "model_fallback": "?"}
    }

# =========================
# Analyse + affichage (sécurisé)
# =========================
def _run_full_pipeline(meta, text):
    title = meta.get("title") or "(sans titre)"
    pubdate = meta.get("pubDate") or ""
    link = meta.get("link") or ""

    if HAS_RICH:
        console.rule("[bold]🆕 Analyse MPC[/bold]")
    else:
        print("\n" + "-"*80); print("🆕 Analyse MPC"); print("-"*80)

    ui_info(f"Titre : {title}")
    ui_info(f"Date  : {pubdate}")
    ui_info(f"Lien  : {link}")

    cleaned_text = _clean_text(text)

    # Basic NLP
    try:
        basic = analyze_tone(cleaned_text or text or "")
        dbg(f"Basic: {basic}")
    except Exception as e:
        basic = {"error": str(e)}
        ui_warn(f"[Basic] Erreur: {e}")

    # Advanced NLP — avec retries/truncations pour garantir un tableau rempli
    adv = _adv_with_retries(cleaned_text or text or "")

    # Affichage joli (Rich sinon stdout)
    try:
        pretty_print_result(adv)
    except Exception as e:
        ui_warn(f"[Affichage avancé] Erreur: {e}")
        print(adv)

    # Extrait source lisible
    excerpt_src = (cleaned_text or text or "")[:1000]
    if len(cleaned_text or text or "") > 1000:
        excerpt_src += "..."
    if HAS_RICH:
        console.print(Panel(Text(excerpt_src), title="Extrait du texte (début)", border_style="blue"))
    else:
        print("\n[Extrait du texte]"); print(excerpt_src)

    # Email (optionnel) — on laisse la gestion d'activation à mailer.py (try/except silencieux)
    try:
        send_notification(meta, basic, adv)
        ui_success("[Watcher] Email envoyé.")
    except Exception as e:
        ui_warn(f"[Watcher] Erreur d'envoi email : {e}")

    return basic, adv

# =========================
# Watcher principal
# =========================
def watch_mpc_summaries():
    state = _load_state()
    ui_header(state)
    last_heartbeat = _now_ts()
    consecutive_errors = 0

    # ① Analyse IMMÉDIATE du dernier article au démarrage
    try:
        meta0 = fetch_latest_mpc_summary()
        if meta0:
            link0 = (meta0.get("link") or "").strip()
            text0 = fetch_summary_text(link0) or ""
            if (text0 or "").strip():
                _run_full_pipeline(meta0, text0)
                # Mémorise pour éviter de ré-analyser en boucle
                state.update({
                    "last_link": link0,
                    "last_pubdate": meta0.get("pubDate") or "",
                    "last_hash": _hash_text(text0),
                    "last_run_ts": _now_ts(),
                })
                _save_state(state)
            else:
                ui_warn("[Init] Pas de texte extractible pour le dernier article.")
        else:
            ui_warn("[Init] Aucun article MPC trouvé.")
    except Exception as e:
        ui_warn(f"[Init] Erreur lors de l'analyse initiale: {e}")

    # ② Boucle NORMALE (n’analyse que les nouveautés)
    try:
        while True:
            try:
                meta = fetch_latest_mpc_summary()
                if meta is None:
                    dbg("fetch_latest_mpc_summary() → None")
            except Exception as e:
                consecutive_errors += 1
                backoff = min(2 ** min(consecutive_errors, 6), 60)
                ui_error(f"[Watcher] Erreur fetch_latest_mpc_summary: {e} → backoff {backoff}s")
                time.sleep(backoff)
                continue

            if meta:
                link = (meta.get("link") or "").strip()
                pubdate = (meta.get("pubDate") or "").strip()
                title = meta.get("title") or "(sans titre)"
                dbg(f"Item: title='{title}', date='{pubdate}', link='{link}'")

                # Texte & hash
                text = ""
                text_hash = None
                if link:
                    try:
                        text = fetch_summary_text(link) or ""
                        text_hash = _hash_text(text)
                        dbg(f"Texte len={len(text)} hash={text_hash[:10]}...")
                    except Exception as e:
                        ui_warn(f"[Watcher] Erreur fetch_summary_text: {e}")

                # Dédup stricte
                same_link = bool(link and link == state.get("last_link"))
                same_date = bool(pubdate and pubdate == state.get("last_pubdate"))
                same_hash = bool(text_hash and text_hash == state.get("last_hash"))
                since_last = _now_ts() - float(state.get("last_run_ts") or 0.0)
                cooldown_ok = since_last >= COOLDOWN_SECONDS

                is_new = False
                if link and text:
                    if same_link and same_date and same_hash:
                        is_new = False
                    elif same_link and same_date and not same_hash:
                        is_new = True
                    elif not same_link:
                        is_new = True

                if is_new and not cooldown_ok:
                    dbg(f"Skip: cooldown ({since_last:.1f}s/{COOLDOWN_SECONDS}s)")
                    is_new = False

                if is_new:
                    _run_full_pipeline(meta, text)
                    state.update({
                        "last_link": link,
                        "last_pubdate": pubdate,
                        "last_hash": text_hash,
                        "last_run_ts": _now_ts(),
                    })
                    _save_state(state)

            # Heartbeat
            now = _now_ts()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                ui_info(f"[Watcher] Heartbeat OK — {stamp}")
                last_heartbeat = now

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        ui_warn("[Watcher] Arrêt demandé. Fin du watcher.")

# =========================
# Entrée
# =========================
if __name__ == "__main__":
    watch_mpc_summaries()
