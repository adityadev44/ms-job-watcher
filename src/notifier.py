"""
Sends matched-job alerts via Telegram and Gmail.

Secrets are read from .env (never from config.yaml or code).
Run `py src/notifier.py --test` to fire a test message through both channels.
"""
from __future__ import annotations

import argparse
import os
import smtplib
from email.message import EmailMessage

import requests
from dotenv import load_dotenv

load_dotenv()

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def format_message(jobs: list[dict]) -> str:
    lines = ["Microsoft job matches:\n"]
    for job in jobs:
        lines.append(f"- {job['title']}")
        lines.append(f"  Location:  {job['location']}")
        lines.append(f"  Posted:    {job['posting_date']}")
        lines.append(f"  Apply:     {job['application_url']}")
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram(message: str, token: str, chat_id: str) -> None:
    url = _TELEGRAM_API.format(token=token)
    response = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=20)
    response.raise_for_status()


def send_email(message: str, gmail_user: str, gmail_password: str, recipients: list[str]) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Microsoft job matches"
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg.set_content(message)

    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(msg)


def notify(jobs: list[dict]) -> None:
    if not jobs:
        return

    message = format_message(jobs)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
    recipients = [r.strip() for r in os.getenv("ALERT_RECIPIENT", "").split(";") if r.strip()]

    if token and chat_id:
        send_telegram(message, token, chat_id)
        print("Telegram alert sent.")
    else:
        print("Telegram skipped (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set).")

    if gmail_user and gmail_password and recipients:
        send_email(message, gmail_user, gmail_password, recipients)
        print(f"Email alert sent to: {', '.join(recipients)}")
    else:
        print("Email skipped (GMAIL_USER, GMAIL_APP_PASSWORD, or ALERT_RECIPIENT not set).")


def _test_message() -> list[dict]:
    return [
        {
            "title": "Senior Software Engineer",
            "location": "India, Telangana, Hyderabad",
            "posting_date": "2026-06-07",
            "application_url": "https://apply.careers.microsoft.com/careers/job/TEST001?domain=microsoft.com",
        }
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Send a test alert through both channels")
    args = parser.parse_args()

    if args.test:
        print("Sending test alert...")
        notify(_test_message())
    else:
        print("Run with --test to send a test alert, or import notify() from your main script.")
