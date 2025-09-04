# file: history_query.py
import json, sys, os

def search(file_path: str, term: str = ""):
    term = (term or "").lower()
    if not os.path.exists(file_path):
        print(f"Fichier introuvable: {file_path}")
        return
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            blob = (j.get("title","") + " " + j.get("summary","") + " " + j.get("insights","")).lower()
            if term in blob:
                print(f"{j.get('pubDate','—')} | {j.get('bank','—')} | {j.get('tone','—')} | {j.get('title','—')}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python history_query.py data/history/fed/events.jsonl [mot-clé]")
        sys.exit(1)
    path = sys.argv[1]
    term = sys.argv[2] if len(sys.argv) > 2 else ""
    search(path, term)
