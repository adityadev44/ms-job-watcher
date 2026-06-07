"""
Tests for src/notifier.py.

All network calls are mocked — nothing is actually sent.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notifier import format_message, notify, send_email, send_telegram

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

ONE_JOB = [
    {
        "title": "Senior Software Engineer",
        "location": "India, Telangana, Hyderabad",
        "posting_date": "2026-06-07",
        "application_url": "https://apply.careers.microsoft.com/careers/job/999?domain=microsoft.com",
    }
]

TWO_JOBS = ONE_JOB + [
    {
        "title": "Software Engineer II",
        "location": "India, Karnataka, Bangalore",
        "posting_date": "2026-06-05",
        "application_url": "https://apply.careers.microsoft.com/careers/job/888?domain=microsoft.com",
    }
]


# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------

def test_format_message_contains_title():
    msg = format_message(ONE_JOB)
    assert "Senior Software Engineer" in msg


def test_format_message_contains_location():
    msg = format_message(ONE_JOB)
    assert "Hyderabad" in msg


def test_format_message_contains_posting_date():
    msg = format_message(ONE_JOB)
    assert "2026-06-07" in msg


def test_format_message_contains_apply_link():
    msg = format_message(ONE_JOB)
    assert "apply.careers.microsoft.com" in msg


def test_format_message_two_jobs():
    msg = format_message(TWO_JOBS)
    assert "Senior Software Engineer" in msg
    assert "Software Engineer II" in msg


# ---------------------------------------------------------------------------
# send_telegram — mock requests.post
# ---------------------------------------------------------------------------

def test_send_telegram_calls_post():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()

    with patch("notifier.requests.post", return_value=fake_response) as mock_post:
        send_telegram("hello", token="fake_token", chat_id="12345")

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "fake_token" in call_kwargs[0][0]  # URL contains the token
    assert call_kwargs[1]["json"]["chat_id"] == "12345"
    assert call_kwargs[1]["json"]["text"] == "hello"


def test_send_telegram_raises_on_http_error():
    fake_response = MagicMock()
    fake_response.raise_for_status.side_effect = Exception("HTTP 401")

    with patch("notifier.requests.post", return_value=fake_response):
        with pytest.raises(Exception, match="HTTP 401"):
            send_telegram("hello", token="bad_token", chat_id="12345")


# ---------------------------------------------------------------------------
# send_email — mock smtplib.SMTP
# ---------------------------------------------------------------------------

def test_send_email_logs_in_and_sends():
    mock_smtp_instance = MagicMock()
    mock_smtp_class = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("notifier.smtplib.SMTP", mock_smtp_class):
        send_email(
            "hello",
            gmail_user="sender@gmail.com",
            gmail_password="app_pw",
            recipients=["receiver@example.com"],
        )

    mock_smtp_instance.starttls.assert_called_once()
    mock_smtp_instance.login.assert_called_once_with("sender@gmail.com", "app_pw")
    mock_smtp_instance.send_message.assert_called_once()


def test_send_email_multiple_recipients():
    mock_smtp_instance = MagicMock()
    mock_smtp_class = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("notifier.smtplib.SMTP", mock_smtp_class):
        send_email(
            "hello",
            gmail_user="sender@gmail.com",
            gmail_password="app_pw",
            recipients=["a@example.com", "b@example.com"],
        )

    sent_msg = mock_smtp_instance.send_message.call_args[0][0]
    assert "a@example.com" in sent_msg["To"]
    assert "b@example.com" in sent_msg["To"]


# ---------------------------------------------------------------------------
# notify — high-level orchestration
# ---------------------------------------------------------------------------

def test_notify_does_nothing_for_empty_list():
    with patch("notifier.send_telegram") as mock_tg, patch("notifier.send_email") as mock_em:
        notify([])
    mock_tg.assert_not_called()
    mock_em.assert_not_called()


def test_notify_calls_both_channels(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "r@example.com")

    with patch("notifier.send_telegram") as mock_tg, patch("notifier.send_email") as mock_em:
        notify(ONE_JOB)

    mock_tg.assert_called_once()
    mock_em.assert_called_once()


def test_notify_skips_telegram_when_token_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "r@example.com")

    with patch("notifier.send_telegram") as mock_tg, patch("notifier.send_email") as mock_em:
        notify(ONE_JOB)

    mock_tg.assert_not_called()
    mock_em.assert_called_once()


def test_notify_skips_email_when_credentials_missing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)

    with patch("notifier.send_telegram") as mock_tg, patch("notifier.send_email") as mock_em:
        notify(ONE_JOB)

    mock_tg.assert_called_once()
    mock_em.assert_not_called()


def test_notify_sends_to_all_semicolon_recipients(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "a@example.com ; b@example.com;c@example.com")

    with patch("notifier.send_telegram"), patch("notifier.send_email") as mock_em:
        notify(ONE_JOB)

    recipients_arg = mock_em.call_args[0][3]
    assert recipients_arg == ["a@example.com", "b@example.com", "c@example.com"]


def test_notify_message_passed_to_both_channels(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "r@example.com")

    with patch("notifier.send_telegram") as mock_tg, patch("notifier.send_email") as mock_em:
        notify(ONE_JOB)

    tg_message = mock_tg.call_args[0][0]
    em_message = mock_em.call_args[0][0]
    assert "Senior Software Engineer" in tg_message
    assert tg_message == em_message
