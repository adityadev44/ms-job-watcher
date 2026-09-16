"""Send a prepared HTML referral digest through the configured Gmail account."""
from __future__ import annotations

import argparse
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--recipient", required=True)
    args = parser.parse_args()

    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    html = Path(args.html).read_text(encoding="utf-8")

    message = EmailMessage()
    message["Subject"] = args.subject
    message["From"] = sender
    message["To"] = args.recipient
    message.set_content(
        "This referral digest is formatted as an HTML table. "
        "Please open it in an HTML-capable email client."
    )
    message.add_alternative(html, subtype="html")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(sender, password)
        smtp.send_message(message)

    print(f"Referral digest sent to {args.recipient}")


if __name__ == "__main__":
    main()
