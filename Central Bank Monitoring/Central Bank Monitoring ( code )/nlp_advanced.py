# nlp_advanced.py
import os
import json
import glob
import torch
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from transformers import pipeline
from dotenv import load_dotenv
from openai import OpenAI
from urllib.parse import urlparse

# ===============================
# 0) Config & ENV
# ===============================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY manquant dans .env")

PRIMARY_MODEL = os.getenv("OPENAI_MODEL_PRIMARY", "gpt-5")
FALLBACK_MODEL = os.getenv("OPENAI_MODEL_FALLBACK", "gpt-4o-mini")

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
print(f"Device set to use {device}")
# Pipelines HF: 0 = GPU/CUDA, -1 = CPU. Sur Mac MPS on laisse -1 (géré par torch en interne).
torch_device = -1

# ===============================
# 1) Pipelines Hugging Face
# ===============================
CANDIDATE_LABELS = ["hawkish", "dovish", "neutral"]
zsc = pipeline("zero-shot-classification", model="facebook/bart-large-mnli", device=torch_device)
summarizer = pipeline("summarization", model="facebook/bart-large-cnn", device=torch_device)

# ===============================
# 2) Utilitaires
# ===============================
def _short_date(date_str: str) -> str:
    return (date_str or "").split("T")[0]

def _smart_summary(text: str) -> str:
    """
    Résumé robuste:
      - segmente par tokens (max ~900 pour BART),
      - résume chaque chunk,
      - re-résume l’assemblage (hiérarchique),
      - fallback sûr en cas d’erreur.
    """
    txt = (text or "").strip()
    if not txt:
        return ""

    # Si le texte est très court, inutile d'appeler le modèle
    if len(txt.split()) < 50:
        return txt

    tok = summarizer.tokenizer
    # BART a un max ~1024 tokens; certains tokenizers exposent une valeur "illimitée" énorme -> reborne à 1024
    model_max = getattr(tok, "model_max_length", 1024)
    if not isinstance(model_max, int) or model_max > 4096 or model_max < 1:
        model_max = 1024
    CHUNK_TOKENS = min(900, model_max - 64)  # marge de sécurité

    try:
        enc = tok(txt, return_tensors=None, truncation=False)
        ids = enc["input_ids"]
        # enc["input_ids"] peut être [[...]] selon la version -> aplatit
        if isinstance(ids, list) and len(ids) and isinstance(ids[0], list):
            ids = ids[0]
    except Exception:
        # fallback: tronque en caractères si tokenizer échoue
        ids = None

    # Crée les chunks (par tokens quand possible, sinon par caractères)
    chunks: list[str] = []
    if isinstance(ids, list):
        for i in range(0, len(ids), CHUNK_TOKENS):
            piece_ids = ids[i:i + CHUNK_TOKENS]
            chunk_text = tok.decode(piece_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True)
            if chunk_text.strip():
                chunks.append(chunk_text)
    else:
        # fallback simple: découpe en blocs de ~4000 caractères
        STEP = 4000
        for i in range(0, len(txt), STEP):
            chunks.append(txt[i:i + STEP])

    # Résume chaque chunk
    summaries: list[str] = []
    for ch in (chunks or [txt]):
        words = ch.split()
        max_len = min(120, max(40, len(words) // 2))
        min_len = max(20, max_len // 2)
        try:
            out = summarizer(
                ch,
                max_length=max_len,
                min_length=min_len,
                do_sample=False,
                truncation=True  # <-- important pour éviter l'overflow
            )
            s = (out[0]["summary_text"] or "").strip()
        except Exception:
            s = ch[:600].strip()  # fallback très simple
        if s:
            summaries.append(s)

    if not summaries:
        return txt[:800]

    if len(summaries) == 1:
        return summaries[0]

    # Re-résume l’assemblage des mini-résumés
    stitched = " ".join(summaries)
    try:
        out2 = summarizer(
            stitched,
            max_length=180,
            min_length=60,
            do_sample=False,
            truncation=True
        )
        return (out2[0]["summary_text"] or "").strip()
    except Exception:
        return stitched[:800]


def load_recent_context(bank: str = "BoE", limit: int = 3, days_window: int = 30) -> List[Dict[str, str]]:
    """
    Fallback historique: lit ./data/*.json et garde les items de ≤ days_window jours.
    Chaque item attendu: { bank, title, date:'YYYY-MM-DD', text, source }
    (N'est utilisé que si context_items n'est PAS fourni)
    """
    ctx: List[Dict[str, str]] = []
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days_window)

    for path in glob.glob("./data/*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data if isinstance(data, list) else data.get("items", [])
                for it in items:
                    btag = (it.get("bank") or it.get("tag") or "").lower()
                    if bank.lower() not in btag:
                        continue
                    raw_date = _short_date(it.get("date", ""))
                    try:
                        parsed = datetime.strptime(raw_date, "%Y-%m-%d").date() if raw_date else None
                    except Exception:
                        parsed = None
                    if not parsed or parsed < cutoff:
                        continue
                    ctx.append({
                        "title": it.get("title", "(sans titre)"),
                        "date": raw_date,
                        "source": it.get("source", bank),
                        "text": (it.get("text", "") or "")[:5000],
                    })
        except Exception:
            pass

    ctx.sort(key=lambda x: x.get("date", ""), reverse=True)

    trimmed = ctx[:limit]
    for c in trimmed:
        txt = c.get("text") or ""
        c["summary"] = _smart_summary(txt) if txt else "(pas de texte)"
    return trimmed

# ===============================
# 3) OpenAI client + helper
# ===============================
client = OpenAI(api_key=OPENAI_API_KEY)

def call_openai_chat(prompt: str, model: Optional[str] = None, max_tokens: int = 450) -> str:
    """
    GPT-5 → max_completion_tokens ; autres → max_tokens. Fallback auto.
    """
    chosen = model or PRIMARY_MODEL

    def _mk_params(which_model: str):
        base = {
            "model": which_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
        }
        if which_model.startswith("gpt-5"):
            base["max_completion_tokens"] = max_tokens
        else:
            base["max_tokens"] = max_tokens
        return base

    try:
        resp = client.chat.completions.create(**_mk_params(chosen))
        return resp.choices[0].message.content.strip()
    except Exception as e1:
        try:
            resp = client.chat.completions.create(**_mk_params(FALLBACK_MODEL))
            return resp.choices[0].message.content.strip() + f"\n\n[Note: fallback → {FALLBACK_MODEL}]"
        except Exception as e2:
            em1, em2 = str(e1).lower(), str(e2).lower()
            if any(k in em1 for k in ["quota","insufficient_quota","ratelimit"]) or any(k in em2 for k in ["quota","insufficient_quota","ratelimit"]):
                return "Insights non disponibles (quota dépassé ou modèle indisponible)."
            return f"[Insight Error] primary={e1} | fallback={e2}"

# ===============================
# 4) Analyses
# ===============================
def generate_basic_insights(text: str) -> Dict[str, Any]:
    """Zero-shot BART-MNLI → labels 'hawkish/dovish/neutral' + scores.
       On tronque l'entrée pour éviter les dépassements."""
    snippet = (text or "")
    if len(snippet) > 4000:
        snippet = snippet[:4000]
    try:
        out = zsc(snippet, candidate_labels=CANDIDATE_LABELS)
        labels = out.get("labels", []) or []
        scores = out.get("scores", []) or []
    except Exception:
        # fallback neutre si le pipeline HF pose problème
        labels, scores = ["neutral", "hawkish", "dovish"], [0.34, 0.33, 0.33]
    top_label = labels[0] if labels else "neutral"
    return {"labels": labels, "scores": scores, "top_label": top_label}


def _normalize_ctx_from_items(context_items: List[Dict[str, str]], bank: str) -> List[Dict[str, str]]:
    """
    Normalise les items venant de cb_sources: [{pubDate,title,link}]
    → [{date,title,source,summary:''}]
    """
    out: List[Dict[str, str]] = []
    for it in (context_items or []):
        out.append({
            "date": it.get("pubDate") or "—",
            "title": it.get("title") or "(sans titre)",
            "source": bank,
            "summary": "",  # on n'a pas le texte ici, donc pas de résumé
        })
    return out

def generate_insights(
    text: str,
    basic_insights: Dict[str, Any],
    summary: str,
    bank: str = "BoE",
    ctx_limit: int = 3,
    context_items: list | None = None
) -> str:
    # Si context_items est fourni, on s'en sert directement
    context_docs: List[Dict[str, str]] = []
    if context_items:
        for it in (context_items or [])[:ctx_limit]:
            pub = (it.get("pubDate") or "").split(" ")[0] if it.get("pubDate") else ""
            link = it.get("link") or ""
            src  = urlparse(link).netloc.replace("www.", "") if link else ""
            context_docs.append({
                "title": it.get("title") or "(sans titre)",
                "date": pub or "non précisé",
                "source": src or "non précisé",
                "text": "",  # on n’a pas de plein texte ici
                "summary": it.get("title") or "(sans titre)",
            })
    else:
        # Sinon, fallback: lecture locale (évite les biais BoE si bank ≠ BoE)
        context_docs = load_recent_context(bank=bank, limit=ctx_limit, days_window=30)

    # Construction bloc contexte pour le prompt
    ctx_lines = []
    for c in context_docs:
        meta = " • ".join([x for x in [c.get("date"), c.get("source"), c.get("title")] if x])
        ctx_lines.append(f"- {meta}\n  {c.get('summary','')}")
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "(Aucun contexte récent disponible)"

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snippet = (text[:2000] + "...") if len(text) > 2000 else text

    prompt = (
        "Tu es un analyste de politique monétaire.\n"
        f"Date actuelle: {today_str}.\n"
        "Règles STRICTES: n'utilise QUE les informations du TEXTE et du CONTEXTE ci-dessous. "
        "N'invente pas de dates/événements. NE CITE AUCUNE DATE DE RÉUNION FUTURE qui n'apparaît pas explicitement. "
        "Si une info est inconnue, écris 'non précisé'.\n\n"
        f"=== TEXTE À ANALYSER ===\n{snippet}\n\n"
        f"=== RÉSUMÉ (HF) ===\n{summary}\n\n"
        f"=== ANALYSE DE BASE (labels/scores) ===\n{basic_insights}\n\n"
        f"=== CONTEXTE RÉCENT {bank} (≤30 jours, plus récents d'abord) ===\n{ctx_block}\n\n"
        "Consignes de sortie:\n"
        "1) TON ABSOLU (hawkish/dovish/neutral) + justification textuelle.\n"
        "2) TON RELATIF vs contexte récent (explique clairement la relativité).\n"
        "3) 3 implications politiques concrètes (timing/probabilité de cut/hike, guidance, dissensus) en puces.\n"
        "4) 'Sources utilisées' listant les titres (ou 'non précisé').\n"
        "Réponds en français, structuré et concis. Donne une forward guidance sur les prochains jours."
    )

    out = call_openai_chat(prompt, model=PRIMARY_MODEL, max_tokens=450)

    # Bloc "Sources utilisées" (optionnel) – on réutilise les 3/5 du contexte
    sources_block = "\n".join([
        f"- {c.get('date','non précisé')} • {c.get('source') or 'non précisé'} • {c.get('title','(sans titre)')}"
        for c in context_docs
    ]) or "(aucun)"
    out += "\n\nSources utilisées (cache ≤30j):\n" + sources_block

    return out

# Export attendu par watcher
def advanced_analyze(text: str, bank: str = "BoE", context_items: list | None = None) -> Dict[str, Any]:
    basic = generate_basic_insights(text)
    summary = _smart_summary(text)
    insights = generate_insights(text, basic, summary, bank=bank, ctx_limit=3, context_items=context_items)
    return {
        "labels": basic["labels"],
        "scores": basic["scores"],
        "tone_abs_no_context": basic["top_label"],
        "summary": summary,
        "insights": insights,
        "meta": {
            "context_window_days": 30,
            "model_primary": PRIMARY_MODEL,
            "model_fallback": FALLBACK_MODEL,
        },
    }


# ===============================
# 5) UI helpers (Rich si dispo)
# ===============================
HAS_RICH = False
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.markdown import Markdown
    from rich.text import Text
    HAS_RICH = True
    _console = Console()
except Exception:
    _console = None

def _hr():
    print("─" * 80)

def pretty_print_result(result: dict, *, suppress_model_sources: bool = False, context_bullets: list | None = None):
    """
    Affichage stylé (Rich si dispo). Paramètres:
      - suppress_model_sources: masque le bloc "Sources utilisées (cache ≤30j)" éventuellement porté par result/meta.
      - context_bullets: liste d'items (dict {'date','title','link'?} ou tuple (date,title))
                         pour afficher tes 5 articles sous la forme "• date • titre".
    """
    labels = result.get("labels", [])
    scores = result.get("scores", [])
    top = result.get("tone_abs_no_context", "neutral")
    summary = result.get("summary", "")
    insights = result.get("insights", "")
    meta = result.get("meta", {})

    # --- Rendu avec Rich
    if HAS_RICH:
        try:
            model_name = meta.get("model_primary") or "?"
            _console.print(Panel(Text("Analyse avancée — Banque centrale", justify="center"),
                                 title=f"Modèle: {model_name}", border_style="cyan"))

            # Tableau numérique
            tbl = Table(title="Résumé numérique")
            tbl.add_column("Champ", style="bold")
            tbl.add_column("Valeur")
            lbl_line = ", ".join(labels) if labels else "—"
            sc_line = ", ".join(f"{s:.2f}" for s in scores) if scores else "—"
            tbl.add_row("Ton absolu", top or "—")
            tbl.add_row("Labels", lbl_line)
            tbl.add_row("Scores", sc_line)
            tbl.add_row("Fenêtre contexte", f"{meta.get('context_window_days', '—')} jours")
            _console.print(tbl)

            # Résumé (HF)
            _console.print(Panel(Text(summary or "(résumé indisponible)"),
                                 title="Résumé (HF)", border_style="green"))

            # Insights (LLM)
            _console.print(Panel(Markdown(insights or "(insights indisponibles)"),
                                 title="Insights", border_style="magenta"))

            # (1) masquer les "sources du modèle" si demandé
            if not suppress_model_sources:
                model_sources = (result.get("sources")
                                 or result.get("meta", {}).get("sources")
                                 or [])
                if model_sources:
                    _console.print(Panel(Text("\n".join(f"• {s}" for s in model_sources)),
                                         title="Sources utilisées (cache ≤30j)",
                                         border_style="grey50"))

            # (2) afficher tes 5 sources de contexte (date • titre)
            if context_bullets:
                lines = []
                for it in context_bullets:
                    if isinstance(it, dict):
                        dd = it.get("date") or it.get("pubDate") or "—"
                        tt = it.get("title") or "—"
                    else:
                        dd, tt = it[0], it[1]
                    lines.append(f"• {dd} • {tt}")
                _console.print(Panel(Text("\n".join(lines)),
                                     title="Sources utilisées (contexte)",
                                     border_style="magenta"))
            return
        except Exception:
            pass  # fallback texte

    # --- Fallback sans Rich
    print(f"== Analyse avancée — Banque centrale (Modèle: {meta.get('model_primary','?')}) ==")
    print("Résumé numérique:")
    print(f"  - Ton absolu      : {top or '—'}")
    print(f"  - Labels          : {', '.join(labels) if labels else '—'}")
    print(f"  - Scores          : {', '.join(f'{s:.2f}' for s in scores) if scores else '—'}")
    print(f"  - Fenêtre contexte: {meta.get('context_window_days', '—')} jours")
    _hr()
    print("Résumé (HF):")
    print(summary or "(résumé indisponible)")
    _hr()
    print("Insights:")
    print(insights or "(insights indisponibles)")
    _hr()

    if not suppress_model_sources:
        model_sources = (result.get("sources")
                         or result.get("meta", {}).get("sources")
                         or [])
        if model_sources:
            print("Sources utilisées (cache ≤30j):")
            for s in model_sources:
                print(f"• {s}")
            _hr()

    if context_bullets:
        print("Sources utilisées (contexte)")
        for it in context_bullets:
            if isinstance(it, dict):
                dd = it.get("date") or it.get("pubDate") or "—"
                tt = it.get("title") or "—"
            else:
                dd, tt = it[0], it[1]
            print(f"• {dd} • {tt}")

# ===============================
# 6) Test local (affichage joli)
# ===============================
if __name__ == "__main__":
    sample_text = (
        "The committee emphasized its data-dependent approach. It noted persistent inflationary pressures "
        "and global uncertainties. Future policy decisions would be guided by incoming data."
    )
    # Démo: pas de context_items → fallback local (probablement vide chez toi)
    result = advanced_analyze(sample_text, bank="BoE")
    pretty_print_result(result, suppress_model_sources=True, context_bullets=[
        {"date": "2025-08-22 2:00AM GMT+2", "title": "Quarterly Financial Report - Second Quarter 2025"},
        {"date": "2025-08-13 2:00AM GMT+2", "title": "Summary of Governing Council deliberations: FAD of July 30, 2025"},
        {"date": "2025-08-11 2:00AM GMT+2", "title": "Market Participants Survey—Second Quarter of 2025"},
        {"date": "2025-07-30 2:00AM GMT+2", "title": "Monetary Policy Report—July 2025"},
        {"date": "2025-07-21 2:00AM GMT+2", "title": "Business Outlook Survey—Second Quarter of 2025"},
    ])
