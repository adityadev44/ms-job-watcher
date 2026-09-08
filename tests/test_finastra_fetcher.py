"""Finastra's Workday CXS pagination, India-token normalization, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import finastra_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def search_response(job_postings, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"total": len(job_postings), "jobPostings": job_postings},
    )


def raw_job(job_id, title, external_path=None, loc_text="Bengaluru",
            posted_on="Posted 3 Days Ago"):
    return {
        "title": title,
        "externalPath": external_path or f"/job/Bengaluru/{title.replace(' ', '-')}_{job_id}",
        "locationsText": loc_text,
        "postedOn": posted_on,
        "bulletFields": [job_id],
    }


def test_fetch_jobs_maps_fields_and_normalizes_india_city(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("REQ0326_0036556", "Senior Back-end Engineer"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20)
    assert len(jobs) == 1
    j = jobs[0]
    assert j['id'] == 'REQ0326_0036556'
    assert j['title'] == 'Senior Back-end Engineer'
    assert j['location'] == 'Bengaluru, India'
    assert j['posting_date'] == (fetcher.date.today() - fetcher.timedelta(days=3)).strftime('%Y-%m-%d')
    assert j['application_url'] == (
        'https://finastra.wd3.myworkdayjobs.com/FINC/job/Bengaluru/Senior-Back-end-Engineer_REQ0326_0036556'
    )

    _, kwargs = post.call_args
    body = kwargs['json']
    assert body['searchText'] == 'software engineer'


def test_non_india_city_is_dropped(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("1", "Senior Sales Executive", loc_text="Dubai"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs == []


def test_indianapolis_is_not_mistaken_for_india(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("1", "Support Engineer", loc_text="Indianapolis, Indiana"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs == []


def test_pune_kept_and_tagged_india_for_downstream_exclusion(monkeypatch):
    # Pune must NOT be dropped by the fetcher -- matcher.py's exclude_locations
    # layer needs the city name present in a job tagged as India to reject it.
    post = Mock(return_value=search_response([
        raw_job("1", "Senior Software Engineer", loc_text="Pune"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert jobs[0]['location'] == 'Pune, India'


def test_job_without_title_or_id_is_skipped(monkeypatch):
    bad_no_title = raw_job("1", "Engineer")
    bad_no_title["title"] = ""
    bad_no_id = raw_job("2", "Engineer")
    bad_no_id["bulletFields"] = []
    bad_no_id["externalPath"] = ""
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
        'https://finastra.wd3.myworkdayjobs.com/FINC/job/Bengaluru/AI-Ops_32118'
    )
    assert description == 'Build with LangChain & RAG.'
    assert posting_date == '2026-08-25'

    args, _ = get.call_args
    assert args[0] == (
        'https://finastra.wd3.myworkdayjobs.com/wday/cxs/finastra/FINC'
        '/job/Bengaluru/AI-Ops_32118'
    )


def test_fetch_job_description_rate_limit_on_429(monkeypatch):
    resp = Mock(status_code=429, raise_for_status=lambda: None)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description(
            'https://finastra.wd3.myworkdayjobs.com/FINC/job/Bengaluru/X_1'
        )
    # A 429 raises immediately without retry (matches the Invesco-template
    # convention) -- only RequestException triggers the one-retry path.
    assert get.call_count == 1
