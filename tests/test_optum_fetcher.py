"""
Tests for src/optum_fetcher.py.

All tests monkeypatch requests.get — no live API calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from optum_fetcher import RateLimitError, _parse_card, fetch_job_description, fetch_jobs

# ---------------------------------------------------------------------------
# Minimal TalentBrew HTML fixtures
# ---------------------------------------------------------------------------

_ONE_JOB_HTML = """
<html><body>
<section id="search-results-list">
<ul>
<li>
<a href="/job/hyderabad/senior-software-engineer/34088/12345678901"
   data-job-id="12345678901" class="brand-facet brand-facet__optum">
<div>
<h2>Senior Software Engineer</h2>
<span class="job-id job-info">SE001</span>
<span class="job-divider"> | </span>
<span class="job-location">Hyderabad, Telangana</span>
</div>
</a>
</li>
</ul>
</section>
</body></html>
"""

_TWO_JOB_HTML = _ONE_JOB_HTML.replace(
    "</ul>",
    """<li>
<a href="/job/chennai/software-engineer/34088/99887766554"
   data-job-id="99887766554" class="brand-facet brand-facet__optum">
<div>
<h2>Software Engineer</h2>
<span class="job-location">Chennai, Tamil Nadu</span>
</div>
</a>
</li>
</ul>""",
)

_EMPTY_RESULTS_HTML = """
<html><body>
<section id="search-results-list"><ul></ul></section>
</body></html>
"""

_DETAIL_HTML = """
<html><body>
<script type="application/ld+json">
{"@type":"JobPosting","datePosted":"2026-6-8",
 "description":"<p>We need a <b>Senior Software Engineer</b> with C# and Azure.</p>"}
</script>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------------------
# _parse_card
# ---------------------------------------------------------------------------

def test_parse_card_extracts_all_fields():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_ONE_JOB_HTML, "html.parser")
    li = soup.find("li")
    card = _parse_card(li)
    assert card["id"] == "12345678901"
    assert card["title"] == "Senior Software Engineer"
    assert card["location"] == "Hyderabad, Telangana"
    assert card["application_url"].startswith("https://careers.unitedhealthgroup.com")
    assert card["posting_date"] == ""


def test_parse_card_returns_none_for_li_without_job_id():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<li><a href='/x'>no data-job-id</a></li>", "html.parser")
    assert _parse_card(soup.find("li")) is None


# ---------------------------------------------------------------------------
# fetch_jobs
# ---------------------------------------------------------------------------

def test_fetch_jobs_returns_job_list(monkeypatch):
    monkeypatch.setattr(
        "optum_fetcher.requests.get",
        lambda *a, **kw: _FakeResponse(_ONE_JOB_HTML),
    )
    jobs = fetch_jobs("software engineer", "India")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Software Engineer"
    assert jobs[0]["id"] == "12345678901"


def test_fetch_jobs_returns_multiple_jobs(monkeypatch):
    monkeypatch.setattr(
        "optum_fetcher.requests.get",
        lambda *a, **kw: _FakeResponse(_TWO_JOB_HTML),
    )
    jobs = fetch_jobs("software engineer", "India")
    assert len(jobs) == 2


def test_fetch_jobs_returns_empty_when_no_results(monkeypatch):
    monkeypatch.setattr(
        "optum_fetcher.requests.get",
        lambda *a, **kw: _FakeResponse(_EMPTY_RESULTS_HTML),
    )
    assert fetch_jobs("software engineer", "India") == []


def test_fetch_jobs_page_from_start_offset(monkeypatch):
    """start=15 must translate to pg=2 in the request params."""
    captured = {}

    def _fake_get(url, headers, params, timeout):
        captured["params"] = params
        return _FakeResponse(_EMPTY_RESULTS_HTML)

    monkeypatch.setattr("optum_fetcher.requests.get", _fake_get)
    fetch_jobs("software engineer", "India", start=15)
    assert captured["params"]["pg"] == 2


def test_fetch_jobs_retries_on_429_then_succeeds(monkeypatch):
    """A 429 response is retried; when the retry succeeds no exception is raised."""
    call_count = 0

    def _fake_get(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeResponse("", status_code=429)
        return _FakeResponse(_ONE_JOB_HTML)

    monkeypatch.setattr("optum_fetcher.time.sleep", lambda s: None)
    monkeypatch.setattr("optum_fetcher.requests.get", _fake_get)

    jobs = fetch_jobs("software engineer", "India")
    assert call_count == 2
    assert len(jobs) == 1


def test_fetch_jobs_raises_rate_limit_after_all_retries(monkeypatch):
    """RateLimitError is raised when every attempt returns 429."""
    monkeypatch.setattr("optum_fetcher.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "optum_fetcher.requests.get",
        lambda *a, **kw: _FakeResponse("", status_code=429),
    )
    with pytest.raises(RateLimitError):
        fetch_jobs("software engineer", "India")


# ---------------------------------------------------------------------------
# fetch_job_description
# ---------------------------------------------------------------------------

def test_fetch_job_description_parses_json_ld(monkeypatch):
    monkeypatch.setattr(
        "optum_fetcher.requests.get",
        lambda *a, **kw: _FakeResponse(_DETAIL_HTML),
    )
    desc = fetch_job_description(
        "https://careers.unitedhealthgroup.com/job/hyderabad/senior-se/34088/12345"
    )
    assert isinstance(desc, str)
    assert "Senior Software Engineer" in desc
    assert "C#" in desc
    assert "<" not in desc  # HTML stripped


def test_fetch_job_description_returns_empty_on_missing_json_ld(monkeypatch):
    monkeypatch.setattr(
        "optum_fetcher.requests.get",
        lambda *a, **kw: _FakeResponse("<html><body>No JSON-LD here</body></html>"),
    )
    assert fetch_job_description("https://careers.unitedhealthgroup.com/job/x/y/34088/1") == ""
