"""FICO's Workday CXS pagination, location-normalization, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import fico_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def search_response(job_postings, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"total": len(job_postings), "jobPostings": job_postings},
    )


def raw_job(job_id, title, external_path=None, loc_text="Bangalore, India",
            posted_on="Posted 3 Days Ago"):
    return {
        "title": title,
        "externalPath": external_path or f"/job/Bangalore-India/{title.replace(' ', '-')}_{job_id}",
        "locationsText": loc_text,
        "postedOn": posted_on,
        "bulletFields": [job_id],
    }


def test_fetch_jobs_maps_fields_and_sends_india_facet(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("32118", "AI Ops Engineer"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20)
    assert len(jobs) == 1
    j = jobs[0]
    assert j['id'] == '32118'
    assert j['title'] == 'AI Ops Engineer'
    assert j['location'] == 'Bangalore, India'
    assert j['posting_date'] == (fetcher.date.today() - fetcher.timedelta(days=3)).strftime('%Y-%m-%d')
    assert j['application_url'] == (
        'https://fico.wd1.myworkdayjobs.com/External/job/Bangalore-India/AI-Ops-Engineer_32118'
    )

    _, kwargs = post.call_args
    body = kwargs['json']
    assert body['appliedFacets'] == {'locations': fetcher._INDIA_LOCATION_WIDS}
    assert body['searchText'] == 'software engineer'


def test_fetch_jobs_dedup_relies_on_stable_ids(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("1", "Engineer A"),
        raw_job("1", "Engineer A"),  # duplicate ID in same page
        raw_job("2", "Engineer B"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    ids = [j['id'] for j in jobs]
    # Fetcher itself doesn't dedup (matcher.py's seen-ID set does); verify
    # IDs are extracted correctly and consistently for the same posting.
    assert ids == ['1', '1', '2']


def test_ambiguous_multi_location_normalized_to_india(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("32184", "Domain Pre Sales AP", loc_text="2 Locations"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs[0]['location'] == 'India'


def test_location_normalization_keeps_genuine_india_text(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("1", "Engineer", loc_text="Work from Home, India"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs[0]['location'] == 'Work from Home, India'


def test_limit_is_capped_at_20(monkeypatch):
    post = Mock(return_value=search_response([]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    fetcher.fetch_jobs('', 'India', num=50)
    _, kwargs = post.call_args
    assert kwargs['json']['limit'] == 20


def test_job_without_title_or_id_is_skipped(monkeypatch):
    bad_no_title = raw_job("1", "Engineer")
    bad_no_title["title"] = ""
    bad_no_id = raw_job("2", "Engineer")
    bad_no_id["bulletFields"] = []
    bad_no_id["externalPath"] = "/job/no-id-here"
    good = raw_job("3", "Real Engineer")

    post = Mock(return_value=search_response([bad_no_title, bad_no_id, good]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert jobs[0]['id'] == '3'


def test_rate_limit_raised_after_persistent_429(monkeypatch):
    resp = Mock(status_code=429, raise_for_status=lambda: None)
    post = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert post.call_count == 3


def test_rate_limit_raised_after_persistent_connection_errors(monkeypatch):
    post = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert post.call_count == 3


def test_transient_failure_then_success_recovers(monkeypatch):
    post = Mock(side_effect=[
        requests.ConnectionError('blip'),
        search_response([raw_job("1", "Engineer")]),
    ])
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert post.call_count == 2


def test_fetch_job_description_strips_html_and_returns_start_date(monkeypatch):
    get = Mock(return_value=Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"jobPostingInfo": {
            "jobDescription": "<p>Build with <b>LangChain</b> &amp; RAG.</p>",
            "startDate": "2026-08-25",
        }},
    ))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, posting_date = fetcher.fetch_job_description(
        'https://fico.wd1.myworkdayjobs.com/External/job/Bangalore-India/AI-Ops_32118'
    )
    assert description == 'Build with LangChain & RAG.'
    assert posting_date == '2026-08-25'

    args, _ = get.call_args
    assert args[0] == (
        'https://fico.wd1.myworkdayjobs.com/wday/cxs/fico/External'
        '/job/Bangalore-India/AI-Ops_32118'
    )


def test_fetch_job_description_rate_limit_on_429(monkeypatch):
    resp = Mock(status_code=429, raise_for_status=lambda: None)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description(
            'https://fico.wd1.myworkdayjobs.com/External/job/Bangalore-India/X_1'
        )
    assert get.call_count == 3
