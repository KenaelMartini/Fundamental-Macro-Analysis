# file: run.py
from scraper import fetch_latest_mpc_summary
from parser import fetch_summary_text
from nlp_advanced import advanced_analyze
from nlp_analyzer import analyze_tone

# Mode terminal uniquement : suppression de l'envoi d'email

def main():
    meta = fetch_latest_mpc_summary()
    if not meta:
        print("Erreur scraping")
        return

    # 1. Affichage des métadonnées
    print(f"Titre   : {meta.get('title', 'Unknown')}")
    print(f"Date    : {meta.get('pubDate', 'Unknown')}")
    print(f"Lien    : {meta.get('link', 'Unknown')}\n")

    # 2. Extraction du texte
    text = fetch_summary_text(meta['link'])
    if not text:
        print("Erreur extraction de texte")
        return

    # 3. Basic NLP
    basic = analyze_tone(text)
    print("🔍 Basic NLP :", basic)

    # 4. Advanced NLP
    adv = advanced_analyze(text)
    print("🔍 Advanced NLP :", adv)

    # 5. Affichage complet du contenu
    print("\n--- Résumé complet ---\n")
    print(text)

if __name__ == '__main__':
    main()
