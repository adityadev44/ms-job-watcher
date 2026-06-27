"""
Sends matched-job alerts via Telegram and Gmail.

Secrets are read from .env (never from config.yaml or code).
Run `py src/notifier.py --test` to fire a test message through both channels.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import tempfile
from email.message import EmailMessage
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_LIMIT = 4096   # Telegram's hard cap on message length in characters
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587
_FAILURES_PATH = Path(__file__).parent.parent / "pipeline_failures.json"
_FAILURE_THRESHOLD = 3


def _read_failures() -> dict:
    try:
        return json.loads(_FAILURES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_failures(data: dict) -> None:
    dir_ = _FAILURES_PATH.parent
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        tmp = f.name
    os.replace(tmp, _FAILURES_PATH)


def format_message(jobs: list[dict], source: str = "Microsoft") -> str:
    lines = [f"{source} job matches:\n"]
    for job in jobs:
        lines.append(f"- {job['title']}")
        lines.append(f"  Location:  {job['location']}")
        lines.append(f"  Posted:    {job['posting_date']}")
        lines.append(f"  Apply:     {job['application_url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _build_telegram_chunks(jobs: list[dict], limit: int = _TELEGRAM_LIMIT, source: str = "Microsoft") -> list[str]:
    """Format jobs into plain-text chunks each shorter than *limit* characters.

    Splits on job boundaries so no individual job is truncated mid-way.
    A single job whose formatted text already exceeds *limit* is sent as-is
    (unavoidable edge-case; single-job messages are always small in practice).
    """
    chunks: list[str] = []
    pending: list[dict] = []
    for job in jobs:
        trial = format_message(pending + [job], source)
        if len(trial) > limit and pending:
            chunks.append(format_message(pending, source))
            pending = [job]
        else:
            pending.append(job)
    if pending:
        chunks.append(format_message(pending, source))
    return chunks


def send_telegram(message: str, token: str, chat_id: str) -> None:
    """Send a single plain-text message to a Telegram chat.

    No parse_mode is set, so Telegram treats the content as plain text and
    special characters (*, _, #, .) in job titles cannot trigger a 400 error.
    Callers are responsible for keeping *message* under _TELEGRAM_LIMIT chars;
    notify() handles that automatically via _build_telegram_chunks().
    """
    url = _TELEGRAM_API.format(token=token)
    response = requests.post(
        url,
        json={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def send_email(message: str, gmail_user: str, gmail_password: str, recipients: list[str], *, source: str = "Microsoft") -> None:
    msg = EmailMessage()
    msg["Subject"] = f"{source} job matches"
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg.set_content(message)

    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(msg)


def notify(jobs: list[dict], source: str = "Microsoft") -> None:
    if not jobs:
        return

    message = format_message(jobs, source)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
    recipients = [r.strip() for r in os.getenv("ALERT_RECIPIENT", "").split(";") if r.strip()]

    chat_ids = [cid.strip() for cid in chat_id.split(",") if cid.strip()]
    if token and chat_ids:
        try:
            chunks = _build_telegram_chunks(jobs, source=source)
            for cid in chat_ids:
                for chunk in chunks:
                    send_telegram(chunk, token, cid)
            print(f"Telegram alert sent ({len(chunks)} message(s)).")
        except Exception as exc:
            print(f"[warn] Telegram alert failed: {exc}")
    else:
        print("Telegram skipped (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set).")

    if gmail_user and gmail_password and recipients:
        try:
            send_email(message, gmail_user, gmail_password, recipients, source=source)
            print(f"Email alert sent to: {', '.join(recipients)}")
        except Exception as exc:
            print(f"[warn] Email alert failed: {exc}")
    else:
        print("Email skipped (GMAIL_USER, GMAIL_APP_PASSWORD, or ALERT_RECIPIENT not set).")


def notify_pipeline_error(source: str, exc: Exception) -> None:
    """Email when a pipeline crashes 3 consecutive times. Silent on 1st/2nd failure."""
    try:
        data = _read_failures()
        data[source] = data.get(source, 0) + 1
        count = data[source]
        _write_failures(data)
        print(f"[{source}] Consecutive failure count: {count}/{_FAILURE_THRESHOLD}")
        if count < _FAILURE_THRESHOLD:
            return
        # Reached threshold — alert, then reset so the next streak also triggers
        data[source] = 0
        _write_failures(data)
        gmail_user = os.getenv("GMAIL_USER", "")
        gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
        recipients = [r.strip() for r in os.getenv("ALERT_RECIPIENT", "").split(";") if r.strip()]
        if gmail_user and gmail_password and recipients:
            send_email(
                f"{source} job-watcher pipeline has failed {_FAILURE_THRESHOLD} consecutive times and did not run.\n\nMost recent error: {exc}",
                gmail_user,
                gmail_password,
                recipients,
                source=f"{source} (pipeline error)",
            )
            print(f"[{source}] Error notification email sent (after {_FAILURE_THRESHOLD} consecutive failures).")
    except Exception:
        pass


def reset_failure_count(source: str) -> None:
    """Reset consecutive failure counter after a successful run. No-ops silently on any error."""
    try:
        data = _read_failures()
        if data.get(source, 0) != 0:
            data[source] = 0
            _write_failures(data)
    except Exception:
        pass


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
