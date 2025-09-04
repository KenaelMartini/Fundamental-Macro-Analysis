# file: scraper.py
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import timezone, timedelta

# Flux RSS filtré pour les MPC summaries uniquement
RSS_FILTERED = (
    "https://www.bankofengland.co.uk/rss/news?"
    "NewsTypes=ce90163e489841e0b66d06243d35d5cb&"
    "Taxonomies=7af03071367c4a5d80cfb86f9c954759&"
    "Direction=Latest"
)

# En-tête HTTP pour simuler un navigateur
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    )
}

def fetch_latest_mpc_summary():
    resp = requests.get(RSS_FILTERED, headers=HEADERS)
    if resp.status_code != 200:
        print(f"Erreur HTTP {resp.status_code}")
        return None
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"Erreur parsing RSS: {e}")
        return None
    channel = root.find('channel')
    if channel is None:
        print("Aucun channel dans le flux RSS.")
        return None
    item = channel.find('item')
    if item is None:
        print("Aucun item RSS trouvé.")
        return None

    title_elem = item.find('title')
    link_elem = item.find('link')
    title = title_elem.text.strip() if title_elem is not None else None
    link = link_elem.text.strip() if link_elem is not None else None

    pub_elem = item.find('pubDate')
    pub_date = None
    if pub_elem is not None and pub_elem.text:
        try:
            dt = parsedate_to_datetime(pub_elem.text.strip())
            paris_tz = timezone(timedelta(hours=2))
            dt_paris = dt.astimezone(paris_tz)
            hour = dt_paris.strftime("%I").lstrip('0')
            minute = dt_paris.strftime("%M")
            am_pm = dt_paris.strftime("%p")
            pub_date = f"{dt_paris.date().isoformat()} {hour}:{minute}{am_pm} GMT+2"
        except Exception:
            pub_date = pub_elem.text.strip()
    return {'title': title, 'link': link, 'pubDate': pub_date}

if __name__ == '__main__':
    data = fetch_latest_mpc_summary()
    if data:
        print("📄 Dernier MPC Summary via RSS :")
        print(f"Titre   : {data['title']}")
        print(f"Date    : {data['pubDate']}")
        print(f"Lien    : {data['link']}")
