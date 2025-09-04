# file: nlp_analyzer.py
from __future__ import annotations
import re
import unicodedata
from typing import List, Dict, Any, Tuple

# -----------------------------------
# Normalisation légère du texte
# -----------------------------------
def _normalize(s: str) -> str:
    if not s:
        return ""
    # lower + suppression des accents (FR), normalisation espaces/traits
    s = s.lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip()
    return s

# -----------------------------------
# Dictionnaire d’expressions (EN/FR)
# poids > 0 = hawkish ; poids < 0 = dovish
# -----------------------------------
# NB: on utilise des regex avec \b pour éviter les sous-chaînes
PATTERNS: List[Tuple[re.Pattern, int, str]] = [
    # HAWKISH
    (re.compile(r"\brate hike(s)?\b"),                         +2, "rate hike"),
    (re.compile(r"\braise(s|d)? (the )?rate(s)?\b"),           +2, "raise rates"),
    (re.compile(r"\bincrease(s|d)? (interest )?rate(s)?\b"),   +2, "increase rates"),
    (re.compile(r"\brestrictive stance\b"),                    +2, "restrictive stance"),
    (re.compile(r"\bfurther (policy )?firming\b"),             +2, "further firming"),
    (re.compile(r"\b(quantitative )?tightening\b"),            +2, "tightening/QT"),
    (re.compile(r"\bpersistent inflation\b"),                  +2, "persistent inflation"),
    (re.compile(r"\belevated inflation\b"),                    +2, "elevated inflation"),
    (re.compile(r"\babove target\b"),                          +2, "above target"),
    (re.compile(r"\bhigher for longer\b"),                     +2, "higher for longer"),
    (re.compile(r"\btight labour market\b|\btight labor market\b"), +2, "tight labour market"),
    (re.compile(r"\binflationary pressures?\b"),               +2, "inflationary pressures"),

    # FR hawkish
    (re.compile(r"\bhausse(s)? de(s)? taux\b"),                +2, "hausse des taux"),
    (re.compile(r"\bresserrement(s)?\b"),                      +2, "resserrement"),
    (re.compile(r"\bposition restrictive\b"),                  +2, "position restrictive"),
    (re.compile(r"\binflation (elevee|elevees|eleve)\b"),      +2, "inflation élevée"),
    (re.compile(r"\bpression(s)? inflationniste(s)?\b"),       +2, "pressions inflationnistes"),

    # DOVISH
    (re.compile(r"\brate cut(s)?\b"),                          -2, "rate cut"),
    (re.compile(r"\blower(s|ed)? (the )?rate(s)?\b"),          -2, "lower rates"),
    (re.compile(r"\bdecrease(s|d)? (interest )?rate(s)?\b"),   -2, "decrease rates"),
    (re.compile(r"\b(accommodative|more accommodative)\b"),    -2, "accommodative"),
    (re.compile(r"\b(quantitative )?easing\b|\bqe\b"),         -2, "easing/QE"),
    (re.compile(r"\bprovide liquidity\b"),                     -1, "provide liquidity"),
    (re.compile(r"\bdisinflation\b|\bcooling inflation\b"),    -2, "disinflation"),
    (re.compile(r"\bbelow target\b"),                          -2, "below target"),
    (re.compile(r"\bdownside risks? to (growth|activity)\b"),  -2, "downside risks to growth"),
    (re.compile(r"\bspare capacity\b|\bslack\b"),              -2, "slack/spare capacity"),
    (re.compile(r"\bsoftening\b|\bmoderating\b|\bwaning\b"),   -1, "softening/moderating"),

    # FR dovish
    (re.compile(r"\bbaisse(s)? de(s)? taux\b"),                -2, "baisse des taux"),
    (re.compile(r"\bassouplissement(s)?\b"),                   -2, "assouplissement"),
    (re.compile(r"\bplus accommodant(e)?\b|\baccommodant(e)?\b"), -2, "accommodant"),
    (re.compile(r"\bdesinflation\b|\breflux de l'inflation\b"), -2, "désinflation"),
    (re.compile(r"\bcapacite(s)? in(ut|u)ilisee(s)?\b|\brelachement\b"), -2, "capacité inutilisée / relâchement"),

    # NEUTRE / atténué (léger +1/-1)
    (re.compile(r"\bdata(-|\s)?dependent\b"),                  +1, "data-dependent"),
    (re.compile(r"\bwait and see\b|\battend(ons|re)\b"),       0,  "wait-and-see"),
    (re.compile(r"\bbalanced risks\b|\brisques equilibres\b"), 0,  "balanced risks"),
]

# Modificateurs de contexte dans une fenêtre proche (±4 mots)
NEGATORS  = re.compile(r"\b(no|not|less|declin\w+|fall\w+|cool\w+|moderating|waning|fading|lower)\b")
BOOSTERS  = re.compile(r"\b(persistent|elevated|sticky|strong|significant|robust|above-target)\b")
DIMINISH  = re.compile(r"\b(temporary|transitory|modest|mild)\b")

# Tokenisation légère pour la fenêtre de contexte
WORD_SPLIT = re.compile(r"\w+|\S")

def _window(text: str, start: int, end: int, radius_words: int = 4) -> str:
    """Retourne le texte ±N mots autour d’un match [start:end]."""
    tokens = list(WORD_SPLIT.finditer(text))
    # localiser l’indice de token du début/fin
    start_tok = next((i for i, m in enumerate(tokens) if m.start() <= start < m.end()), 0)
    end_tok   = next((i for i, m in enumerate(tokens) if m.start() < end <= m.end()), len(tokens)-1)
    lo = max(0, start_tok - radius_words)
    hi = min(len(tokens), end_tok + radius_words + 1)
    return text[tokens[lo].start(): tokens[hi-1].end()] if tokens else text[max(0, start-50): end+50]

def analyze_tone(text: str) -> Dict[str, Any]:
    """
    Retourne:
      - score (int)
      - tone ('hawkish'/'dovish'/'neutral')
      - keywords (liste des libellés déclenchés)
      - details (liste enrichie avec positions/poids ajustés)
    """
    raw = text or ""
    norm = _normalize(raw)
    score = 0
    keywords: List[str] = []
    details: List[Dict[str, Any]] = []

    for rx, w0, label in PATTERNS:
        for m in rx.finditer(norm):
            adj = w0
            ctx = _window(norm, m.start(), m.end(), radius_words=4)

            # Modulateurs locaux
            if NEGATORS.search(ctx):
                adj = -adj  # inverse si on trouve une négation/affaiblissement proche
            if adj > 0 and BOOSTERS.search(ctx):
                adj += 1
            if adj < 0 and BOOSTERS.search(ctx):
                adj -= 0  # un booster avec un terme dovish n'inverse pas, on laisse tel quel
            if DIMINISH.search(ctx):
                # atténue l'intensité absolue
                if adj > 0:
                    adj -= 1
                elif adj < 0:
                    adj += 1

            score += adj
            keywords.append(label)
            details.append({
                "label": label,
                "base_weight": w0,
                "adjusted_weight": adj,
                "match": norm[m.start():m.end()],
                "context": ctx
            })

    # Tonalité finale (seuil simple pour rester compatible)
    tone = "hawkish" if score > 0 else ("dovish" if score < 0 else "neutral")

    return {
        "score": score,
        "tone": tone,
        "keywords": keywords,
        "details": details  # champ additionnel, optionnel à exploiter
    }

if __name__ == "__main__":
    samples = [
        "The Committee remains data-dependent but vigilant about persistent inflation. Further policy firming may be appropriate.",
        "Le Conseil adopte une approche accommodante; l'inflation reflue et l'activité se modère. Baisse de taux à l'étude.",
        "Balanced risks and a wait-and-see approach. Inflationary pressures are moderating, not persistent."
    ]
    for s in samples:
        print("\n=== SAMPLE ===")
        print(s)
        print(analyze_tone(s))
