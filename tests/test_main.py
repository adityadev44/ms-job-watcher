"""
Tests for src/main.py.

All external I/O (Microsoft API, Telegram, email) is mocked.
seen_jobs.json is written to a temporary directory so tests never touch the real file.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import cli_main, load_seen_ids, run_pipeline, save_seen_ids
from notifier import DeliveryResult

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

GOOD_JOB = {
    "id": "200099001",
    "title": "Senior Software Engineer",
    "location": "India, Telangana, Hyderabad",
    "posting_date": "2026-06-07",
    "application_url": "https://apply.careers.microsoft.com/careers/job/1970393556999001?domain=microsoft.com",
}

ANOTHER_JOB = {
    "id": "200099002",
    "title": "Software Engineer II",
    "location": "India, Karnataka, Bangalore",
    "posting_date": "2026-06-06",
    "application_url": "https://apply.careers.microsoft.com/careers/job/1970393556999002?domain=microsoft.com",
}


# ---------------------------------------------------------------------------
# load_seen_ids / save_seen_ids — unit tests
# ---------------------------------------------------------------------------

def test_load_seen_ids_returns_empty_set_when_file_missing(tmp_path):
    ids = load_seen_ids(tmp_path / "nonexistent.json")
    assert ids == set()


def test_load_seen_ids_reads_existing_file(tmp_path):
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps(["aaa", "bbb"]))
    assert load_seen_ids(seen) == {"aaa", "bbb"}


def test_save_seen_ids_writes_sorted_list(tmp_path):
    seen = tmp_path / "seen.json"
    save_seen_ids(seen, {"zzz", "aaa", "mmm"})
    saved = json.loads(seen.read_text())
    assert saved == ["aaa", "mmm", "zzz"]


def test_save_then_load_roundtrips(tmp_path):
    seen = tmp_path / "seen.json"
    original = {"id1", "id2", "id3"}
    save_seen_ids(seen, original)
    assert load_seen_ids(seen) == original


# ---------------------------------------------------------------------------
# run_pipeline — already-seen job is skipped
# ---------------------------------------------------------------------------

def test_known_job_is_skipped(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"
    seen_path.write_text(json.dumps([GOOD_JOB["id"]]))

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB])), \
         patch("main.notify") as mock_notify:
        result = run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    mock_notify.assert_not_called()
    assert result["new"] == 0
    assert result["alert_sent"] is False


def test_seen_ids_are_passed_to_matcher_for_early_pruning(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"
    seen_path.write_text(json.dumps([GOOD_JOB["id"]]))

    with patch("main.find_matching_jobs", return_value=(10, [])) as mock_find:
        run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    assert mock_find.call_args.kwargs["known_ids"] == {GOOD_JOB["id"]}


# ---------------------------------------------------------------------------
# run_pipeline — new job triggers alert and gets recorded
# ---------------------------------------------------------------------------

def test_new_job_triggers_alert(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB])), \
         patch("main.notify") as mock_notify:
        result = run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    mock_notify.assert_called_once()
    assert result["new"] == 1
    assert result["alert_sent"] is True


def test_new_job_is_recorded_in_seen_file(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB])), \
         patch("main.notify"):
        run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    saved = json.loads(seen_path.read_text())
    assert GOOD_JOB["id"] in saved


def test_all_delivery_attempts_failed_does_not_record_job(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"
    failed = DeliveryResult(telegram_attempted=True)

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB])), \
         patch("main.notify", return_value=failed):
        result = run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    assert not seen_path.exists()
    assert result["new"] == 1
    assert result["alert_sent"] is False


def test_one_successful_channel_records_job(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"
    partial = DeliveryResult(
        telegram_attempted=True,
        email_attempted=True,
        email_succeeded=True,
    )

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB])), \
         patch("main.notify", return_value=partial):
        result = run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    assert load_seen_ids(seen_path) == {GOOD_JOB["id"]}
    assert result["alert_sent"] is True


def test_notify_receives_only_new_jobs(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"
    seen_path.write_text(json.dumps([GOOD_JOB["id"]]))

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB, ANOTHER_JOB])), \
         patch("main.notify") as mock_notify:
        result = run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    mock_notify.assert_called_once()
    alerted_jobs = mock_notify.call_args[0][0]
    assert len(alerted_jobs) == 1
    assert alerted_jobs[0]["id"] == ANOTHER_JOB["id"]
    assert result["new"] == 1


# ---------------------------------------------------------------------------
# run_pipeline — second run with same jobs sends nothing
# ---------------------------------------------------------------------------

def test_second_run_sends_nothing(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB])), \
         patch("main.notify"):
        run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    with patch("main.find_matching_jobs", return_value=(10, [GOOD_JOB])), \
         patch("main.notify") as mock_notify:
        result = run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    mock_notify.assert_not_called()
    assert result["new"] == 0
    assert result["alert_sent"] is False


# ---------------------------------------------------------------------------
# run_pipeline — zero matches sends nothing
# ---------------------------------------------------------------------------

def test_no_matches_sends_nothing(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"

    with patch("main.find_matching_jobs", return_value=(10, [])), \
         patch("main.notify") as mock_notify:
        result = run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    mock_notify.assert_not_called()
    assert result["alert_sent"] is False


def test_no_matches_does_not_write_seen_file(tmp_path):
    seen_path = tmp_path / "seen_jobs.json"

    with patch("main.find_matching_jobs", return_value=(10, [])), \
         patch("main.notify"):
        run_pipeline(config_path=CONFIG_PATH, seen_path=seen_path)

    assert not seen_path.exists()


def test_cli_main_returns_nonzero_and_reports_pipeline_error():
    exc = RuntimeError("boom")
    with patch("main.run_pipeline", side_effect=exc), \
         patch("main.notify_pipeline_error") as report:
        status = cli_main()

    assert status == 1
    report.assert_called_once_with("Microsoft", exc)


def test_cli_main_returns_zero_and_resets_failure_count():
    with patch("main.run_pipeline"), patch("main.reset_failure_count") as reset:
        status = cli_main()

    assert status == 0
    reset.assert_called_once_with("Microsoft")
