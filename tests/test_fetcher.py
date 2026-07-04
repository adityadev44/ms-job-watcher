"""
Tests for src/fetcher.py.

All tests use the saved sample_response.json fixture and never call the live API.
"""

import json
import sys
from pathlib import Path

import pytest

# Make sure the src package is importable when running pytest from the project root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fetcher import RateLimitError, _parse_position, fetch_job_description, fetch_jobs

SAMPLE_FILE = Path(__file__).parent / "sample_response.json"


@pytest.fixture()
def sample_response() -> dict:
    with SAMPLE_FILE.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture()
def raw_positions(sample_response) -> list:
    return sample_response["data"]["positions"]


# ---------------------------------------------------------------------------
# _parse_position: unit tests against a single raw position dict
# ---------------------------------------------------------------------------


def test_parse_extracts_id(raw_positions):
    job = _parse_position(raw_positions[0])
    assert job["id"] == raw_positions[0]["displayJobId"]


def test_parse_extracts_title(raw_positions):
    job = _parse_position(raw_positions[0])
    assert job["title"] == raw_positions[0]["name"]
    assert len(job["title"]) > 0


def test_parse_extracts_location(raw_positions):
    job = _parse_position(raw_positions[0])
    # Location should be a non-empty string joining the raw locations list
    assert isinstance(job["location"], str)
    assert len(job["location"]) > 0
    # Every raw location entry should appear somewhere in the joined string
    for loc in raw_positions[0]["locations"]:
        assert loc in job["location"]


def test_parse_extracts_posting_date(raw_positions):
    job = _parse_position(raw_positions[0])
    # Should be an ISO date string: YYYY-MM-DD
    assert len(job["posting_date"]) == 10
    assert job["posting_date"][4] == "-"
    assert job["posting_date"][7] == "-"


def test_parse_extracts_application_url(raw_positions):
    job = _parse_position(raw_positions[0])
    assert job["application_url"].startswith("https://apply.careers.microsoft.com")
    assert "domain=microsoft.com" in job["application_url"]
    assert str(raw_positions[0]["id"]) in job["application_url"]


def test_parse_all_five_fields_present(raw_positions):
    """Each parsed job must contain exactly the five required fields."""
    required = {"id", "title", "location", "posting_date", "application_url"}
    for raw in raw_positions:
        job = _parse_position(raw)
        assert required.issubset(job.keys()), f"Missing fields in: {job}"


# ---------------------------------------------------------------------------
# fetch_jobs: integration test against the saved sample (no live API)
# ---------------------------------------------------------------------------


def test_fetch_jobs_parses_sample(monkeypatch, sample_response):
    """fetch_jobs should return clean dicts when the HTTP layer is patched."""

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return sample_response

    def _fake_get(*args, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr("fetcher.requests.get", _fake_get)

    jobs = fetch_jobs("software engineer", "India")

    assert len(jobs) == len(sample_response["data"]["positions"])
    for job in jobs:
        assert job["id"]
        assert job["title"]
        assert job["location"]
        assert job["posting_date"]
        assert job["application_url"].startswith("https://apply.careers.microsoft.com")


def test_fetch_jobs_sends_sort_by_param(monkeypatch, sample_response):
    """sortBy must be included in the request params so the API can date-sort."""
    captured = {}

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return sample_response

    def _fake_get(*args, **kwargs):
        captured["params"] = kwargs.get("params", {})
        return _FakeResponse()

    monkeypatch.setattr("fetcher.requests.get", _fake_get)
    fetch_jobs("software engineer", "India")

    assert "sortBy" in captured["params"], "sortBy must be sent to the API"
    assert captured["params"]["sortBy"] == "date"


def test_fetch_jobs_sort_by_override(monkeypatch, sample_response):
    """Callers can override the sort_by value."""
    captured = {}

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return sample_response

    def _fake_get(*args, **kwargs):
        captured["params"] = kwargs.get("params", {})
        return _FakeResponse()

    monkeypatch.setattr("fetcher.requests.get", _fake_get)
    fetch_jobs("software engineer", "India", sort_by="relevance")

    assert captured["params"]["sortBy"] == "relevance"


# ---------------------------------------------------------------------------
# 429 / rate-limit handling
# ---------------------------------------------------------------------------


def test_fetch_jobs_retries_on_429_then_succeeds(monkeypatch, sample_response):
    """A single 429 is retried; when the retry succeeds no exception is raised."""
    call_count = 0

    class _FakeRateLimited:
        status_code = 429
        def raise_for_status(self): pass
        def json(self): return {}

    class _FakeOK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return sample_response

    def _fake_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeRateLimited() if call_count == 1 else _FakeOK()

    monkeypatch.setattr("fetcher.time.sleep", lambda s: None)
    monkeypatch.setattr("fetcher.requests.get", _fake_get)

    jobs = fetch_jobs("software engineer", "India")

    assert call_count == 2          # retried exactly once
    assert len(jobs) > 0            # results returned from the successful attempt


def test_fetch_jobs_raises_rate_limit_after_all_retries(monkeypatch):
    """RateLimitError is raised when every attempt returns 429."""
    class _FakeRateLimited:
        status_code = 429
        def raise_for_status(self): pass
        def json(self): return {}

    monkeypatch.setattr("fetcher.time.sleep", lambda s: None)
    monkeypatch.setattr("fetcher.requests.get", lambda *a, **kw: _FakeRateLimited())

    with pytest.raises(RateLimitError):
        fetch_jobs("software engineer", "India")


def test_fetch_job_description_returns_normalized_plain_text(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"job_description": "<p>C# &amp; <strong>.NET</strong></p>"}

    captured = {}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs["params"]
        return _FakeResponse()

    monkeypatch.setattr("fetcher.requests.get", _fake_get)

    description = fetch_job_description(
        "https://apply.careers.microsoft.com/careers/job/12345?domain=microsoft.com"
    )

    assert description == "C# & .NET"
    assert captured["url"].endswith("/12345")
    assert captured["params"] == {"domain": "microsoft.com"}
