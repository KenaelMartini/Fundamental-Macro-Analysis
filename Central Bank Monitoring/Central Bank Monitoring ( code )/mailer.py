# file: mailer.py
import os, smtplib
from email.mime.text import MIMEText

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
RECIPIENT = os.getenv("RECIPIENT")

def send_notification(meta, basic, adv):
    if not all([SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, RECIPIENT]):
        raise RuntimeError("SMTP non configuré (variables d'environnement manquantes).")

    subject = f"Analyse MPC : {meta.get('title','')} ({meta.get('pubDate','')})"
    body = "\n".join([
        f"Titre : {meta.get('title','')}",
        f"Date  : {meta.get('pubDate','')}",
        f"Lien  : {meta.get('link','')}",
        "",
        "🔍 Basic NLP :",
        str(basic),
        "",
        "🔍 Advanced NLP :",
        str(adv)
    ])

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = RECIPIENT

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
