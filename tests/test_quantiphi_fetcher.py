"""Quantiphi Workday fetcher: facet request shape, location normalization,
ID extraction, and failure contracts (mocked HTTP — no real network calls)."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import quantiphi_fetcher as fetcher


def _search_response(postings):
    resp = Mock(status_code=200)
    resp.json.return_value = {"jobPostings": postings}
    resp.raise_for_status = Mock()
    return resp


def test_fetch_jobs_parses_id_title_location(monkeypatch):
    postings = [
        {
            "title": "Senior Machine Learning Engineer",
            "externalPath": "/job/IN-MH-Mumbai-Eureka/Senior-Machine-Learning-Engineer_JR11938",
            "locationsText": "IN MH Mumbai Eureka",
            "postedOn": "Posted Today",
            "bulletFields": ["JR11938"],
        }
    ]
    post = Mock(return_value=_search_response(postings))
    monkeypatch.setattr(fetcher.requests, "post", post)

    jobs = fetcher.fetch_jobs("machine learning", "India", num=20, start=0)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "JR11938"
    assert job["location"] == "IN MH Mumbai Eureka, India"
    assert job["application_url"] == (
        "https://quantiphi.wd1.myworkdayjobs.com/Careers_at_Quantiphi"
        "/job/IN-MH-Mumbai-Eureka/Senior-Machine-Learning-Engineer_JR11938"
    )

    body = post.call_args.kwargs["json"]
    assert set(body["appliedFacets"]["locations"]) == set(fetcher._INDIA_WIDS)
    assert body["searchText"] == "machine learning"


@pytest.mark.parametrize("locations_text,expected", [
    ("IN KA Bengaluru", "IN KA Bengaluru, India"),
    ("IN KL Trivandrum", "IN KL Trivandrum, India"),   # excluded downstream by config
    ("2 Locations", "2 Locations, India"),
    ("", "India"),
])
def test_location_always_gets_india_appended(monkeypatch, locations_text, expected):
    postings = [{
        "title": "Senior Engineer", "externalPath": "/job/x/Senior-Engineer_JR1",
        "locationsText": locations_text, "postedOn": "Posted Today",
        "bulletFields": ["JR1"],
    }]
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response(postings)))
    jobs = fetcher.fetch_jobs("engineer", "India", num=20, start=0)
    assert jobs[0]["location"] == expected


def test_missing_bullet_field_falls_back_to_external_path_suffix(monkeypatch):
    postings = [{
        "title": "Data Engineer",
        "externalPath": "/job/IN-KA-Bengaluru/Data-Engineer_JR99999",
        "locationsText": "IN KA Bengaluru", "postedOn": "Posted 3 Days Ago",
        "bulletFields": [],
    }]
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response(postings)))
    jobs = fetcher.fetch_jobs("engineer", "India", num=20, start=0)
    assert jobs[0]["id"] == "JR99999"


def test_job_without_any_id_is_skipped(monkeypatch):
    postings = [{
        "title": "No ID Job", "externalPath": "/job/x/No-Id-Job",
        "locationsText": "IN KA Bengaluru", "postedOn": "", "bulletFields": [],
    }]
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response(postings)))
    assert fetcher.fetch_jobs("engineer", "India", num=20, start=0) == []


def test_fetch_job_description_strips_html_and_returns_start_date(monkeypatch):
    resp = Mock(status_code=200)
    resp.json.return_value = {
        "jobPostingInfo": {
            "jobDescription": "<p>Build <b>GenAI</b> systems.</p>",
            "startDate": "2026-09-07",
        }
    }
    resp.raise_for_status = Mock()
    monkeypatch.setattr(fetcher.requests, "get", Mock(return_value=resp))

    desc, date = fetcher.fetch_job_description(
        "https://quantiphi.wd1.myworkdayjobs.com/Careers_at_Quantiphi"
        "/job/IN-KA-Bengaluru/Data-Engineer_JR99999"
    )
    assert desc == "Build GenAI systems."
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
