"""Clearwater Analytics' Workday pagination, India-location-normalization,
dedup, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import clearwater_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def search_response(job_postings, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"total": len(job_postings), "jobPostings": job_postings},
    )


def raw_posting(job_id, title, location_text="Office - Bengaluru", posted_on="Posted Today",
                 path=None):
    office = location_text.replace(', India', '').replace(' ', '-')
    path = path or f"/job/{office}/{title.replace(' ', '-')}_{job_id}"
    return {
        "title": title,
        "externalPath": path,
        "timeType": "Full time",
        "locationsText": location_text,
        "postedOn": posted_on,
        "bulletFields": [job_id],
    }


def test_fetch_jobs_maps_fields_and_appends_india(monkeypatch):
    post = Mock(return_value=search_response([
        raw_posting("R12243", "Software Development Engineer", "Office - Noida", "Posted Today"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20, start=0)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "R12243"
    assert job["title"] == "Software Development Engineer"
    # locationsText never says "India" on this tenant -- appended client-side.
    assert job["location"] == "Office - Noida, India"
    assert job["posting_date"] == fetcher.date.today().strftime("%Y-%m-%d")
    assert job["application_url"] == (
        "https://clearwateranalytics.wd1.myworkdayjobs.com/Clearwater_Analytics_Careers"
        "/job/Office---Noida/Software-Development-Engineer_R12243"
    )


def test_fetch_jobs_sends_india_location_facet_and_caps_limit(monkeypatch):
    post = Mock(return_value=search_response([]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    fetcher.fetch_jobs('anything', 'India', num=999, start=40)
    _, kwargs = post.call_args
    body = kwargs['json']
    assert body["appliedFacets"] == {"locations": fetcher._INDIA_LOCATION_WIDS}
    # Tenant hard-caps limit at 20 -- fetcher must never send more.
    assert body["limit"] == 20
    assert body["offset"] == 40
    # Keyword is ignored server-side per this tenant's confirmed narrowing bug.
    assert body["searchText"] == ""


def test_pagination_via_start_offset(monkeypatch):
    page1 = search_response([raw_posting(f"R{i}", "Engineer") for i in range(20)])
    page2 = search_response([raw_posting(f"R{i}", "Engineer") for i in range(20, 25)])
    post = Mock(side_effect=[page1, page2])
    monkeypatch.setattr(fetcher.requests, 'post', post)

    first = fetcher.fetch_jobs('', '', num=20, start=0)
    second = fetcher.fetch_jobs('', '', num=20, start=20)
    assert len(first) == 20
    assert len(second) == 5
    assert post.call_count == 2


def test_dedup_by_id_left_to_caller_but_ids_are_stable(monkeypatch):
    # matcher.py dedups by id across pages; verify id extraction is exact
    # and stable across repeated postings of the same job.
    post = Mock(return_value=search_response([
        raw_posting("R1", "Engineer"),
        raw_posting("R1", "Engineer"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    jobs = fetcher.fetch_jobs('', '')
    assert [j["id"] for j in jobs] == ["R1", "R1"]


def test_posting_without_bullet_fields_is_skipped(monkeypatch):
    posting = raw_posting("R1", "Engineer")
    posting["bulletFields"] = []
    post = Mock(return_value=search_response([posting]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    assert fetcher.fetch_jobs('', '') == []


def test_posting_without_title_is_skipped(monkeypatch):
    posting = raw_posting("R1", "Engineer")
    posting["title"] = ""
    post = Mock(return_value=search_response([posting]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    assert fetcher.fetch_jobs('', '') == []


def test_multi_location_rollup_still_gets_india_appended(monkeypatch):
    post = Mock(return_value=search_response([
        raw_posting("R1", "Engineer", location_text="2 Locations"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    jobs = fetcher.fetch_jobs('', '')
    assert jobs[0]["location"] == "2 Locations, India"


def test_location_already_saying_india_not_doubled(monkeypatch):
    post = Mock(return_value=search_response([
        raw_posting("R1", "Engineer", location_text="Office - Noida, India"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    jobs = fetcher.fetch_jobs('', '')
    assert jobs[0]["location"] == "Office - Noida, India"


def test_indiana_style_substring_does_not_false_positive_word_boundary():
    # Guard exists even though this tenant's own office names never trigger
    # it -- confirms the regex is genuinely word-boundary aware.
    assert not fetcher._INDIA_WORD_RE.search("Indianapolis, US")
    assert fetcher._INDIA_WORD_RE.search("Bengaluru, India")


def test_rate_limit_raised_on_429(monkeypatch):
    resp = Mock(status_code=429)
    post = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    assert post.call_count == 3  # 3 attempts before raising


def test_rate_limit_raised_on_persistent_connection_failure(monkeypatch):
    post = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    assert post.call_count == 3


def test_transient_failure_then_success_recovers(monkeypatch):
    good = search_response([raw_posting("R1", "Engineer")])
    post = Mock(side_effect=[requests.ConnectionError('blip'), good])
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 1
    assert post.call_count == 2


def test_fetch_job_description_parses_html_and_iso_date(monkeypatch):
    detail = Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {
            "jobPostingInfo": {
                "jobDescription": "<div><p>Build <b>scalable</b> systems.</p></div>",
                "startDate": "2026-08-14",
            }
        },
    )
    get = Mock(return_value=detail)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    url = (
        "https://clearwateranalytics.wd1.myworkdayjobs.com/Clearwater_Analytics_Careers"
        "/job/Office---Noida/Software-Development-Engineer_R5299"
    )
    description, posting_date = fetcher.fetch_job_description(url)
    assert description == "Build scalable systems."
    assert posting_date == "2026-08-14"
    api_url, kwargs = get.call_args
    assert api_url[0] == (
        "https://clearwateranalytics.wd1.myworkdayjobs.com/wday/cxs/clearwateranalytics"
        "/Clearwater_Analytics_Careers/job/Office---Noida/Software-Development-Engineer_R5299"
    )


def test_fetch_job_description_rate_limit_on_429(monkeypatch):
    resp = Mock(status_code=429)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description("https://example.com/Clearwater_Analytics_Careers/job/x")


def test_fetch_job_description_retries_once_then_raises(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description("https://example.com/Clearwater_Analytics_Careers/job/x")
    assert get.call_count == 2
