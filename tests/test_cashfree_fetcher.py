"""Cashfree's Kula ATS cache-once pagination, inline descriptions, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import cashfree_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


@pytest.fixture(autouse=True)
def reset_cache():
    fetcher._cache_filled = False
    fetcher._job_cache = []
    fetcher._description_cache = {}
    yield
    fetcher._cache_filled = False
    fetcher._job_cache = []
    fetcher._description_cache = {}


def api_response(data, page=1, pages=1, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {
            "data": data,
            "meta": {"count": len(data), "page": page, "items": 100, "pages": pages},
            "errors": [],
        },
    )


def raw_job(job_id, title, description="<p>Build things.</p>",
            offices=None, launch_at="2026-05-08T09:10:29.000Z"):
    return {
        "id": job_id,
        "title": title,
        "listed": True,
        "launch_at": launch_at,
        "ats_job": {
            "job_description": description,
            "offices": offices if offices is not None else [
                {"location": "Bellandur, Karnataka, India", "country": "India",
                 "city": "Bellandur", "state": "Karnataka", "remote": False}
            ],
        },
    }


def test_fetch_jobs_maps_fields_from_single_page(monkeypatch):
    get = Mock(return_value=api_response([raw_job(201, "Software Development Engineer 3")]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20)
    assert len(jobs) == 1
    j = jobs[0]
    assert j['id'] == '201'
    assert j['title'] == 'Software Development Engineer 3'
    assert j['location'] == 'Bellandur, Karnataka, India'
    assert j['posting_date'] == '2026-05-08'
    assert j['application_url'] == 'https://careers.kula.ai/cashfree/201'

    params = get.call_args.kwargs['params']
    assert params['accountName'] == 'cashfree'


def test_fetch_jobs_paginates_until_meta_pages_exhausted(monkeypatch):
    page1 = api_response([raw_job(1, "Job One")], page=1, pages=2)
    page2 = api_response([raw_job(2, "Job Two")], page=2, pages=2)
    get = Mock(side_effect=[page1, page2])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India', num=20)
    assert {j['id'] for j in jobs} == {'1', '2'}
    assert get.call_count == 2


def test_cache_is_filled_only_once_across_multiple_calls(monkeypatch):
    get = Mock(return_value=api_response([raw_job(1, "Job One")]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    fetcher.fetch_jobs('a', 'India')
    fetcher.fetch_jobs('b', 'India')
    fetcher.fetch_jobs('c', 'India')
    assert get.call_count == 1


def test_job_without_title_or_id_is_skipped(monkeypatch):
    bad_no_title = raw_job(1, "")
    bad_no_id = raw_job(None, "Engineer")
    good = raw_job(3, "Real Engineer")
    get = Mock(return_value=api_response([bad_no_title, bad_no_id, good]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert jobs[0]['id'] == '3'


def test_rate_limit_raised_after_persistent_429(monkeypatch):
    resp = Mock(status_code=429, raise_for_status=lambda: None)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert get.call_count == 3


def test_rate_limit_raised_after_persistent_connection_errors(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert get.call_count == 3


def test_fetch_job_description_served_from_inline_cache(monkeypatch):
    get = Mock(return_value=api_response([
        raw_job(201, "Software Development Engineer 3",
                description="<p>Work with <b>C#</b> &amp; .NET.</p>"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, posting_date = fetcher.fetch_job_description(
        'https://careers.kula.ai/cashfree/201'
    )
    assert description == 'Work with C# & .NET.'
    assert posting_date == '2026-05-08'
    # No extra HTTP call beyond the one cache-fill request.
    assert get.call_count == 1
