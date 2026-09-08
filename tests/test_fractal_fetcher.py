"""Fractal Analytics Workday fetcher: facet request shape, location
normalization, ID extraction, and failure contracts (mocked HTTP — no real
network calls)."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import fractal_fetcher as fetcher


def _search_response(postings):
    resp = Mock(status_code=200)
    resp.json.return_value = {"jobPostings": postings}
    resp.raise_for_status = Mock()
    return resp


def test_fetch_jobs_parses_id_title_location(monkeypatch):
    postings = [
        {
            "title": "Full Stack Senior Engineer (React+Python)",
            "externalPath": "/job/Bengaluru/Full-Stack-Senior-Engineer--React-Python-_SR-45534",
            "locationsText": "Bengaluru",
            "postedOn": "Posted Today",
            "bulletFields": ["SR-45534"],
        }
    ]
    post = Mock(return_value=_search_response(postings))
    monkeypatch.setattr(fetcher.requests, "post", post)

    jobs = fetcher.fetch_jobs("python", "India", num=20, start=0)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "SR-45534"
    assert job["location"] == "Bengaluru, India"
    assert job["posting_date"] == __import__("datetime").date.today().strftime("%Y-%m-%d")
    assert job["application_url"] == (
        "https://fractal.wd1.myworkdayjobs.com/Careers"
        "/job/Bengaluru/Full-Stack-Senior-Engineer--React-Python-_SR-45534"
    )

    # India-only city WIDs must be sent as the location facet.
    body = post.call_args.kwargs["json"]
    assert set(body["appliedFacets"]["locations"]) == set(fetcher._INDIA_WIDS)
    assert body["searchText"] == "python"


@pytest.mark.parametrize("locations_text,expected", [
    ("Bengaluru", "Bengaluru, India"),
    ("Pune", "Pune, India"),               # excluded city still labeled India by the fetcher
    ("Chennai", "Chennai, India"),
    ("5 Locations", "5 Locations, India"),
    ("", "India"),
])
def test_location_always_gets_india_appended(monkeypatch, locations_text, expected):
    postings = [{
        "title": "Senior Engineer", "externalPath": "/job/x/Senior-Engineer_SR-1",
        "locationsText": locations_text, "postedOn": "Posted Today",
        "bulletFields": ["SR-1"],
    }]
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response(postings)))
    jobs = fetcher.fetch_jobs("engineer", "India", num=20, start=0)
    assert jobs[0]["location"] == expected


def test_missing_bullet_field_falls_back_to_external_path_suffix(monkeypatch):
    postings = [{
        "title": "Data Engineer",
        "externalPath": "/job/Bengaluru/Data-Engineer_SR-99999",
        "locationsText": "Bengaluru", "postedOn": "Posted 3 Days Ago",
        "bulletFields": [],
    }]
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response(postings)))
    jobs = fetcher.fetch_jobs("engineer", "India", num=20, start=0)
    assert jobs[0]["id"] == "SR-99999"


def test_job_without_any_id_is_skipped(monkeypatch):
    postings = [{
        "title": "No ID Job", "externalPath": "/job/x/No-Id-Job",
        "locationsText": "Bengaluru", "postedOn": "", "bulletFields": [],
    }]
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response(postings)))
    assert fetcher.fetch_jobs("engineer", "India", num=20, start=0) == []


def test_fetch_job_description_strips_html_and_returns_start_date(monkeypatch):
    resp = Mock(status_code=200)
    resp.json.return_value = {
        "jobPostingInfo": {
            "jobDescription": "<p>Build <b>ML</b> systems.</p>",
            "startDate": "2026-09-07",
        }
    }
    resp.raise_for_status = Mock()
    monkeypatch.setattr(fetcher.requests, "get", Mock(return_value=resp))

    desc, date = fetcher.fetch_job_description(
        "https://fractal.wd1.myworkdayjobs.com/Careers/job/Bengaluru/Data-Engineer_SR-99999"
    )
    assert desc == "Build ML systems."
    assert date == "2026-09-07"


def test_429_raises_ratelimiterror(monkeypatch):
    resp = Mock(status_code=429)
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=resp))
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs("engineer", "India", num=20, start=0)


def test_connection_failure_raises_ratelimiterror_after_retries(monkeypatch):
    monkeypatch.setattr(fetcher.requests, "post", Mock(side_effect=requests.ConnectionError("down")))
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs("engineer", "India", num=20, start=0)
