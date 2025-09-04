# tools/gen_te_yaml.py
from pathlib import Path
import yaml

COUNTRIES = [
    ("United States", "US"),
    ("Euro Area", "EA"),
    ("Canada", "CA"),
    ("United Kingdom", "UK"),
    ("Japan", "JP"),
    ("Switzerland", "CH"),
    ("Australia", "AU"),
    ("New Zealand", "NZ"),
]

INDICATORS = [
    ("GDP Growth Rate",          "GDP-GROWTH",        60),
    ("GDP Annual Growth Rate",   "GDP-ANNUAL",       120),
    ("Unemployment Rate",        "UNEMP",              2),   # tu peux monter à 5–10 si besoin
    ("Inflation Rate",           "CPI-YOY",            2),
    ("Inflation Rate Mom",       "CPI-MOM",            2),
    ("Core Inflation Rate",      "CORE-CPI",           2),
    ("Interest Rate",            "RATE",              60),
    ("Balance of Trade",         "TRADE-BAL",        180),
    ("Business Confidence",      "BUSINESS-CONF",    180),
    ("Manufacturing PMI",        "PMI-MFG",            3),
    ("Services PMI",             "PMI-SERV",           3),
    ("Consumer Confidence",      "CONSUMER-CONF",    180),
    ("Retail Sales MoM",         "RETAIL-MOM",        60),
    ("Building Permits",         "BUILDING-PERMITS", 120),
]

def build_rows():
    rows = []
    for country, code in COUNTRIES:
        for name, short, poll in INDICATORS:
            # NFP: US seulement
            if short == "NFP":
                continue
            rows.append({
                "id": f"{code}-{short}",
                "provider": "TE",
                "country": country,
                "indicator": name,
                "poll_every_sec": poll
            })
        # Ajouter NFP pour US seulement
        if code == "US":
            rows.append({
                "id": "US-NFP",
                "provider": "TE",
                "country": "United States",
                "indicator": "Non Farm Payrolls",
                "poll_every_sec": 2   # 1–2 s : agressif mais raisonnable
            })

    return rows

def main():
    out = Path("config/econ_te.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump(rows, f, allow_unicode=True, sort_keys=False)
    print(f"Wrote {out} with {len(rows)} sources.")

if __name__ == "__main__":
    main()
