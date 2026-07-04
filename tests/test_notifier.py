"""
Tests for src/notifier.py.

All network calls are mocked — nothing is actually sent.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notifier import (
    DeliveryResult,
    _build_telegram_chunks,
    format_message,
    notify,
    notify_pipeline_error,
    send_email,
    send_telegram,
)

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

# 30 jobs whose combined message exceeds Telegram's 4096-char limit
MANY_JOBS = [
    {
        "title": f"Software Engineer {i}",
        "location": "India, Telangana, Hyderabad",
        "posting_date": "2026-06-08",
        "application_url": (
            f"https://apply.careers.microsoft.com/careers/job/{i:06d}?domain=microsoft.com"
        ),
    }
    for i in range(30)
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
# _build_telegram_chunks
# ---------------------------------------------------------------------------

def test_build_telegram_chunks_single_chunk_for_small_batch():
    """A small batch must fit in exactly one chunk."""
    chunks = _build_telegram_chunks(ONE_JOB)
    assert len(chunks) == 1
    assert "Senior Software Engineer" in chunks[0]


def test_build_telegram_chunks_all_jobs_present():
    """Every job must appear in exactly one chunk of the output."""
    chunks = _build_telegram_chunks(MANY_JOBS)
    combined = "\n".join(chunks)
    for job in MANY_JOBS:
        assert job["title"] in combined


def test_build_telegram_chunks_splits_large_batch():
    """30 jobs must be split into multiple chunks, each within the 4096-char limit."""
    assert len(format_message(MANY_JOBS)) > 4096, "Precondition: 30 jobs must exceed limit"
    chunks = _build_telegram_chunks(MANY_JOBS)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096, f"Chunk length {len(chunk)} exceeds 4096"


def test_build_telegram_chunks_custom_limit():
    """A tight custom limit causes more, smaller chunks."""
    chunks_tight = _build_telegram_chunks(TWO_JOBS, limit=300)
    chunks_loose = _build_telegram_chunks(TWO_JOBS, limit=4096)
    assert len(chunks_tight) >= len(chunks_loose)


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


def test_send_telegram_no_parse_mode():
    """Telegram payload must not include parse_mode so special chars can't cause a 400."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()

    with patch("notifier.requests.post", return_value=fake_response) as mock_post:
        send_telegram("hello", token="fake_token", chat_id="12345")

    payload = mock_post.call_args[1]["json"]
    assert "parse_mode" not in payload


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
        result = notify([])
    mock_tg.assert_not_called()
    mock_em.assert_not_called()
    assert result == DeliveryResult()


def test_notify_calls_both_channels(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "r@example.com")

    with patch("notifier.send_telegram") as mock_tg, patch("notifier.send_email") as mock_em:
        result = notify(ONE_JOB)

    mock_tg.assert_called_once()
    mock_em.assert_called_once()
    assert result.telegram_attempted and result.telegram_succeeded
    assert result.email_attempted and result.email_succeeded
    assert result.should_mark_seen


def test_notify_all_configured_channels_failed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "r@example.com")

    with patch("notifier.send_telegram", side_effect=Exception("telegram down")), \
         patch("notifier.send_email", side_effect=Exception("email down")):
        result = notify(ONE_JOB)

    assert result.any_attempted
    assert not result.any_succeeded
    assert not result.should_mark_seen


def test_notify_no_config_preserves_local_seen_semantics(monkeypatch):
    for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GMAIL_USER",
        "GMAIL_APP_PASSWORD", "ALERT_RECIPIENT",
    ):
        monkeypatch.delenv(name, raising=False)

    result = notify(ONE_JOB)

    assert not result.any_attempted
    assert result.should_mark_seen


def test_notify_partial_config_does_not_advance_seen_state(monkeypatch):
    for name in (
        "TELEGRAM_CHAT_ID", "GMAIL_USER", "GMAIL_APP_PASSWORD", "ALERT_RECIPIENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-without-chat")

    result = notify(ONE_JOB)

    assert result.telegram_attempted
    assert not result.any_succeeded
    assert not result.should_mark_seen


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
    assert tg_message == em_message  # ONE_JOB fits in a single chunk


def test_notify_splits_long_message_into_chunks(monkeypatch):
    """30 jobs (> 4096 chars combined) must be sent as multiple Telegram messages."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)

    assert len(format_message(MANY_JOBS)) > 4096, "Precondition: 30 jobs must exceed limit"

    with patch("notifier.send_telegram") as mock_tg:
        notify(MANY_JOBS)

    assert mock_tg.call_count > 1, "Expected multiple send_telegram calls for large batch"
    for call in mock_tg.call_args_list:
        chunk_text = call[0][0]
        assert len(chunk_text) <= 4096, f"Chunk length {len(chunk_text)} exceeds 4096"


def test_notify_telegram_failure_does_not_block_email(monkeypatch):
    """A Telegram send error must not crash the run or prevent the email being sent."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "r@example.com")

    with patch("notifier.send_telegram", side_effect=Exception("400 Bad Request")), \
         patch("notifier.send_email") as mock_em:
        notify(ONE_JOB)   # must not raise

    mock_em.assert_called_once()


def test_notify_email_failure_does_not_block_telegram(monkeypatch):
    """An email send error must not crash the run or prevent the Telegram alert."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALERT_RECIPIENT", "r@example.com")

    with patch("notifier.send_telegram") as mock_tg, \
         patch("notifier.send_email", side_effect=Exception("SMTP connection refused")):
        notify(ONE_JOB)   # must not raise

    mock_tg.assert_called_once()


def test_pipeline_failure_updates_are_thread_safe(monkeypatch, tmp_path):
    """Concurrent companies must not erase each other's failure counters."""
    failures_path = tmp_path / "pipeline_failures.json"
    monkeypatch.setattr("notifier._FAILURES_PATH", failures_path)
    monkeypatch.setattr("notifier._FAILURE_THRESHOLD", 100)

    # Widen the historical read/write race window. The notifier lock should
    # still serialize each complete transaction.
    import notifier
    original_read = notifier._read_failures

    def slow_read():
        data = original_read()
        time.sleep(0.002)
        return data

    monkeypatch.setattr("notifier._read_failures", slow_read)
    sources = [f"Company {index}" for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda source: notify_pipeline_error(source, RuntimeError("x")), sources))

    assert json.loads(failures_path.read_text(encoding="utf-8")) == {
        source: 1 for source in sources
    }
