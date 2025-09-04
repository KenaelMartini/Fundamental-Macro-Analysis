import os, requests

COUNTRY = "United States"
INDICATOR = "Inflation Rate"

def call_with_token_header(token):
    url = "https://api.tradingeconomics.com/indicators"
    r = requests.get(url, params={"country": COUNTRY, "indicator": INDICATOR},
                     headers={"X-TE-API-KEY": token}, timeout=10)
    return ("token_header", r)

def call_with_c_param(value):
    url = "https://api.tradingeconomics.com/indicators"
    r = requests.get(url, params={"country": COUNTRY, "indicator": INDICATOR, "c": value},
                     timeout=10)
    return ("c_param", r)

def pretty(resp):
    try:
        j = resp.json()
    except Exception:
        print("HTTP", resp.status_code, resp.text[:200])
        return
    first = j[0] if j else {}
    print("HTTP", resp.status_code, "| Category:", first.get("Category"),
          "| LatestValue:", first.get("LatestValue"),
          "| LatestValueDate:", first.get("LatestValueDate"))

token  = os.environ.get("TE_TOKEN")
client = os.environ.get("TE_CLIENT")
secret = os.environ.get("TE_SECRET")

print("\n=== 1) Token en header X-TE-API-KEY ===")
if token:
    name, r = call_with_token_header(token)
    pretty(r)
else:
    print("Pas de TE_TOKEN dans l'env")

print("\n=== 2) c=client:secret (ancien mode) ===")
if client and secret:
    name, r = call_with_c_param(f"{client}:{secret}")
    pretty(r)
else:
    print("Pas de TE_CLIENT/TE_SECRET")

print("\n=== 3) c=guest:guest (diagnostic) ===")
name, r = call_with_c_param("guest:guest")
pretty(r)
