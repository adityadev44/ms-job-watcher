"""Recovered-source contracts: genuine zero != failed or partial inventory."""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def iso():
    return importlib.reload(importlib.import_module("isolved_fetcher"))


@pytest.fixture
def citizens():
    return importlib.reload(importlib.import_module("citizensfinancial_fetcher"))


def response(data):
    return SimpleNamespace(json=lambda: data)


def test_isolved_structured_description_and_country(iso, monkeypatch):
    url = iso.BASE + "/jobs/123.html"
    listing = f'Displaying 2 listing(s)<a href="{url}">Software Engineer Hyderabad, IND</a><a href="{iso.BASE}/jobs/124.html">Engineer Indianapolis, USA</a>'
    job = {"@type": "JobPosting", "title": "Software Engineer", "description": "<p>Develop C# applications.</p>",
           "datePosted": "2026-10-09 00:00:00", "jobLocation": {"address": {"addressLocality": "Hyderabad", "addressRegion": "TS", "addressCountry": "IN"}}}
    calls = []
    def get(url, timeout):
        calls.append(url)
        return SimpleNamespace(text=listing if "jobsandemployment" in url else '<script type="application/ld+json">' + json.dumps(job) + '</script>')
    monkeypatch.setattr(iso, "get", get)
    jobs = iso.fetch_jobs("ignored", "India")
    assert len(jobs) == 1
    assert jobs[0]["location"] == "Hyderabad, TS, India"
    assert iso.fetch_job_description(url) == ("Develop C# applications.", "2026-10-09")
    assert iso.fetch_jobs("different", "India", start=1) == []
    assert len(calls) == 2


@pytest.mark.parametrize("html", ["Maintenance", 'Displaying 2 listing(s)<a href="https://isolved.isolvedhire.com/jobs/1.html">USA</a>'])
def test_isolved_invalid_inventory_is_sticky_error(iso, monkeypatch, html):
    calls = []
    monkeypatch.setattr(iso, "get", lambda *a: calls.append(a) or SimpleNamespace(text=html))
    for _ in range(2):
        with pytest.raises(iso.RateLimitError):
            iso.fetch_jobs("", "India")
    assert len(calls) == 1


def test_citizens_paginate_then_filter_not_keyword_guess(citizens, monkeypatch):
    pages = [
        {"items": [{"TotalJobsCount": 3, "requisitionList": [
            {"Id": "1", "Title": "Indiana engineer", "PrimaryLocationCountry": "US", "PrimaryLocation": "Indiana, United States"},
            {"Id": "2", "Title": "Remote engineer", "PrimaryLocationCountry": "US", "PrimaryLocation": "Remote"}]}]},
        {"items": [{"TotalJobsCount": 3, "requisitionList": [
            {"Id": "3", "Title": "Software Engineer", "PrimaryLocationCountry": "IN", "PrimaryLocation": "Hyderabad", "PostedDate": "2026-10-09"}]}]},
    ]
    calls = []
    def get(url, timeout, **kwargs):
        calls.append(kwargs["params"]["finder"])
        return response(pages.pop(0))
    monkeypatch.setattr(citizens, "get", get)
    jobs = citizens.fetch_jobs("India", "India")
    assert [j["id"] for j in jobs] == ["3"]
    assert jobs[0]["location"] == "Hyderabad, India"
    assert "offset=2" in calls[1]
    assert "keyword=" not in calls[0]
    assert citizens.fetch_jobs("another", "India") == jobs
    assert len(calls) == 2


def test_citizens_real_zero(citizens, monkeypatch):
    monkeypatch.setattr(citizens, "get", lambda *a, **kw: response({"items": [{"TotalJobsCount": 0, "requisitionList": []}]}))
    assert citizens.fetch_jobs("", "India") == []


@pytest.mark.parametrize("data", [{}, {"items": []}, {"items": [{"TotalJobsCount": 4, "requisitionList": []}]}])
def test_citizens_failed_inventory_is_sticky(citizens, monkeypatch, data):
    calls = []
    monkeypatch.setattr(citizens, "get", lambda *a, **kw: calls.append(a) or response(data))
    for _ in range(2):
        with pytest.raises(citizens.RateLimitError):
            citizens.fetch_jobs("", "India")
    assert len(calls) == 1


def test_citizens_repeat_page_raises(citizens, monkeypatch):
    monkeypatch.setattr(citizens, "get", lambda *a, **kw: response({"items": [{"TotalJobsCount": 3, "requisitionList": [{"Id": "1"}]}]}))
    with pytest.raises(citizens.RateLimitError, match="repeated"):
        citizens.fetch_jobs("", "India")


def test_citizens_secondary_country_and_detail(citizens, monkeypatch):
    assert citizens._india_locations({"PrimaryLocationCountry": "US", "PrimaryLocation": "New York", "secondaryLocations": [{"Country": "IN", "LocationName": "Bengaluru"}]}) == "Bengaluru, India"
    monkeypatch.setattr(citizens, "get", lambda *a, **kw: response({"items": [{"ExternalDescriptionStr": "<p>C# engineering</p>", "ExternalResponsibilitiesStr": "<p>Build services</p>", "ExternalPostedStartDate": "2026-10-09T00:00:00"}]}))
    assert citizens.fetch_job_description(citizens.JOB_BASE + "123") == ("C# engineering Build services", "2026-10-09")
    with pytest.raises(citizens.RateLimitError):
        citizens.fetch_job_description("https://example.com/123")


@pytest.mark.parametrize("slug", ["isolved", "citizensfinancial"])
@pytest.mark.parametrize("delivered", [True, False])
def test_pipeline_delivery_and_state_are_injected(slug, delivered, monkeypatch, tmp_path):
    import run_company
    job = {"id": "test-1", "title": "Software Engineer", "location": "Hyderabad, India",
           "description": "C# engineering", "posting_date": "2026-10-09", "application_url": "https://example.com", "tags": [".NET / C#"]}
    monkeypatch.setattr(run_company, "find_matching_jobs", lambda *a, **kw: (1, [job]))
    calls = []
    def notifier(jobs, *, source):
        calls.append((jobs, source))
        return delivered
    state = tmp_path / "seen.json"
    run_company.run_company_pipeline(slug, seen_path=state, notify_func=notifier, fetcher=SimpleNamespace())
    assert len(calls) == 1
    assert state.exists() == delivered
    if delivered:
        assert json.loads(state.read_text()) == ["test-1"]


def test_http_retry_exhaustion(monkeypatch):
    import deferred_source_http as http
    import requests
    calls = []
    def fail(*a, **kw):
        calls.append(a)
        raise requests.HTTPError("503 unavailable")
    monkeypatch.setattr(http.requests, "get", fail)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    with pytest.raises(http.RateLimitError):
        http.get("https://example.com")
    assert len(calls) == 3
