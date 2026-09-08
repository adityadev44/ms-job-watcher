"""Tredence RippleHire fetcher: search-result parsing, India location
normalization (locations vs jobLocation, whitelist/non-India tokens), JSON
detail-response parsing, and failure contracts (mocked HTTP — no real
network calls)."""
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import tredence_fetcher as fetcher


def _search_response(job_vo_list):
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json.return_value = {"jobVoList": job_vo_list, "totalJobCount": len(job_vo_list)}
    return resp


def test_fetch_jobs_parses_id_title_location(monkeypatch):
    jobs = [{
        "jobSeq": "902892", "jobTitle": "AI Engineer (Backend)",
        "jobLocation": "Bangalore", "locations": "Bangalore",
    }]
    post = Mock(return_value=_search_response(jobs))
    monkeypatch.setattr(fetcher.requests, "post", post)

    result = fetcher.fetch_jobs("ai engineer", "India", num=20, start=0)
    assert len(result) == 1
    job = result[0]
    assert job["id"] == "902892"
    assert job["title"] == "AI Engineer (Backend)"
    assert job["location"] == "Bangalore, India"
    assert job["application_url"] == (
        "https://tredence.ripplehire.com/candidate/"
        "?token=rzuz0vttMaz0VxxVzDiY&lang=en&source=CAREERSITE#detail/job/902892"
    )

    sent = json.loads(post.call_args.kwargs["data"]["careerSiteUrlParams"])
    assert sent["search"] == "ai engineer"
    assert sent["token"] == fetcher._TOKEN


def test_empty_keyword_short_circuits_without_a_request(monkeypatch):
    post = Mock()
    monkeypatch.setattr(fetcher.requests, "post", post)
    assert fetcher.fetch_jobs("", "India", num=20, start=0) == []
    post.assert_not_called()


@pytest.mark.parametrize("locations,job_location,expected", [
    ("Bangalore", None, "Bangalore, India"),
    ("Gurgaon", None, "Gurgaon, India"),
    ("Pune", None, "Pune, India"),          # excluded downstream by config
    ("Kolkata", None, "Kolkata, India"),    # excluded downstream by config
    ("San Jose (TR)", None, "San Jose (TR)"),   # non-India, left untouched
    ("Toronto", None, "Toronto"),
    (None, "Bangalore", "Bangalore, India"),   # falls back to jobLocation
])
def test_location_normalization(monkeypatch, locations, job_location, expected):
    job = {"jobSeq": "1", "jobTitle": "X"}
    if locations is not None:
        job["locations"] = locations
    if job_location is not None:
        job["jobLocation"] = job_location
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response([job])))
    result = fetcher.fetch_jobs("engineer", "India", num=20, start=0)
    assert result[0]["location"] == expected


def test_job_missing_id_or_title_is_skipped(monkeypatch):
    jobs = [
        {"jobSeq": "1", "jobTitle": "", "locations": "Bangalore"},
        {"jobSeq": None, "jobTitle": "X", "locations": "Bangalore"},
    ]
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=_search_response(jobs)))
    assert fetcher.fetch_jobs("engineer", "India", num=20, start=0) == []


def test_fetch_job_description_parses_json_and_strips_html(monkeypatch):
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json.return_value = {
        "jobVO": {
            "jobDesc": "<p>Build <b>LangGraph</b> agents.</p>",
            "jobSkills": "",
            "jobPostingDate": "20-Aug-2026",
        }
    }
    monkeypatch.setattr(fetcher.requests, "get", Mock(return_value=resp))

    desc, date = fetcher.fetch_job_description(
        "https://tredence.ripplehire.com/candidate/"
        "?token=rzuz0vttMaz0VxxVzDiY&lang=en&source=CAREERSITE#detail/job/902892"
    )
    assert desc == "Build LangGraph agents."
    assert date == "2026-08-20"


def test_non_json_detail_response_raises_ratelimiterror(monkeypatch):
    resp = Mock(status_code=200)
    resp.raise_for_status = Mock()
    resp.json.side_effect = ValueError("not json")
    monkeypatch.setattr(fetcher.requests, "get", Mock(return_value=resp))
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description("...#detail/job/1")


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
