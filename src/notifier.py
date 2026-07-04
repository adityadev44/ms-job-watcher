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
import threading
from dataclasses import dataclass
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
_FAILURES_LOCK = threading.RLock()


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of an alert attempt, suitable for deciding deduplication state.

    With no configured channels, ``should_mark_seen`` is true to preserve the
    historical local-development behaviour.  If one or more channels are
    configured, at least one must succeed before jobs should be marked seen.
    """

    telegram_attempted: bool = False
    telegram_succeeded: bool = False
    email_attempted: bool = False
    email_succeeded: bool = False

    @property
    def any_attempted(self) -> bool:
        return self.telegram_attempted or self.email_attempted

    @property
    def any_succeeded(self) -> bool:
        return self.telegram_succeeded or self.email_succeeded

    @property
    def should_mark_seen(self) -> bool:
        return not self.any_attempted or self.any_succeeded


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


def notify(jobs: list[dict], source: str = "Microsoft") -> DeliveryResult:
    if not jobs:
        return DeliveryResult()

    message = format_message(jobs, source)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
    recipients = [r.strip() for r in os.getenv("ALERT_RECIPIENT", "").split(";") if r.strip()]

    chat_ids = [cid.strip() for cid in chat_id.split(",") if cid.strip()]
    # Treat partially supplied credentials as a configured-but-failed channel.
    # Otherwise one missing secret would silently advance deduplication state.
    telegram_attempted = bool(token or chat_ids)
    telegram_succeeded = False
    if token and chat_ids:
        chunks = _build_telegram_chunks(jobs, source=source)
        failures = 0
        successes = 0
        for cid in chat_ids:
            recipient_succeeded = True
            for chunk in chunks:
                try:
                    send_telegram(chunk, token, cid)
                    successes += 1
                except Exception as exc:
                    recipient_succeeded = False
                    failures += 1
                    print(f"[warn] Telegram alert failed for chat {cid}: {exc}")
            # The Telegram channel counts as successful only if at least one
            # recipient received every chunk (and therefore every job).
            telegram_succeeded = telegram_succeeded or recipient_succeeded
        if successes:
            print(f"Telegram alert sent ({successes} message(s), {failures} failed).")
    else:
        print("Telegram skipped (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set).")

    email_attempted = bool(gmail_user or gmail_password or recipients)
    email_succeeded = False
    if gmail_user and gmail_password and recipients:
        try:
            send_email(message, gmail_user, gmail_password, recipients, source=source)
            email_succeeded = True
            print(f"Email alert sent to: {', '.join(recipients)}")
        except Exception as exc:
            print(f"[warn] Email alert failed: {exc}")
    else:
        print("Email skipped (GMAIL_USER, GMAIL_APP_PASSWORD, or ALERT_RECIPIENT not set).")

    return DeliveryResult(
        telegram_attempted=telegram_attempted,
        telegram_succeeded=telegram_succeeded,
        email_attempted=email_attempted,
        email_succeeded=email_succeeded,
    )


def notify_pipeline_error(source: str, exc: Exception) -> None:
    """Email when a pipeline crashes 3 consecutive times. Silent on 1st/2nd failure."""
    try:
        # The launcher runs companies in threads, so protect the complete
        # read-modify-write transaction from lost updates.
        with _FAILURES_LOCK:
            data = _read_failures()
            data[source] = data.get(source, 0) + 1
            count = data[source]
            _write_failures(data)
            if count >= _FAILURE_THRESHOLD:
                # Reset in the same transaction so another thread cannot
                # overwrite it with a stale snapshot.
                data[source] = 0
                _write_failures(data)
        print(f"[{source}] Consecutive failure count: {count}/{_FAILURE_THRESHOLD}")
        if count < _FAILURE_THRESHOLD:
            return
        # Reached threshold — alert after releasing the state lock so a slow
        # SMTP connection cannot block unrelated pipeline state updates.
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
        with _FAILURES_LOCK:
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
