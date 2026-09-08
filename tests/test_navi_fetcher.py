"""Navi's TurboHire-via-first-party-proxy fetcher: single-request caching,
inline description parsing, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import navi_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache():
    fetcher._job_cache.clear()
    fetcher._detail_cache.clear()
    fetcher._cache_filled = False
    fetcher._cache_error = None
    yield
    fetcher._job_cache.clear()
    fetcher._detail_cache.clear()
    fetcher._cache_filled = False
    fetcher._cache_error = None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def list_response(jobs, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"Total": len(jobs), "FilteredCount": len(jobs), "Jobs": jobs},
    )


def raw_job(job_id, title, address="Bangalore, Karnataka, India",
            description="<p>Build <b>things</b>.</p>", published="2026-08-25T10:11:22Z",
            apply_url=None):
    return {
        "JobId": job_id,
        "JobTitle": title,
        "JobDescriptionV2": description,
        "Location": [{"Address": address, "PlaceId": None}],
        "PublishedDate": published,
        "ApplyUrl": apply_url or f"https://navi.turbohire.co/job/publicjobs/{job_id}?utm_source=CareerPage",
    }


def test_fetch_jobs_maps_fields(monkeypatch):
    get = Mock(return_value=list_response([raw_job("abc-123", "SDE III - Security")]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('engineer', 'India')
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "abc-123"
    assert job["title"] == "SDE III - Security"
    assert job["location"] == "Bangalore, Karnataka, India"
    assert job["posting_date"] == "2026-08-25"
    assert job["application_url"] == (
        "https://navi.turbohire.co/job/publicjobs/abc-123?utm_source=CareerPage"
    )


def test_fetch_jobs_caches_and_only_calls_once(monkeypatch):
    get = Mock(return_value=list_response([raw_job("j1", "Engineer")]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    fetcher.fetch_jobs('anything', 'India')
    fetcher.fetch_jobs('another keyword entirely', 'India')
    assert get.call_count == 1


def test_pagination_via_start_num(monkeypatch):
    jobs = [raw_job(f"j{i}", "Engineer") for i in range(25)]
    get = Mock(return_value=list_response(jobs))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    first = fetcher.fetch_jobs('', '', num=20, start=0)
    second = fetcher.fetch_jobs('', '', num=20, start=20)
    assert len(first) == 20
    assert len(second) == 5
    assert get.call_count == 1  # cached after first fill


def test_job_missing_id_or_title_is_skipped(monkeypatch):
    missing_id = raw_job("", "Engineer")
    missing_title = raw_job("j2", "")
    get = Mock(return_value=list_response([missing_id, missing_title]))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    assert fetcher.fetch_jobs('', '') == []


def test_location_falls_back_to_india_when_address_missing(monkeypatch):
    job = raw_job("j1", "Engineer")
    job["Location"] = []
    get = Mock(return_value=list_response([job]))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    jobs = fetcher.fetch_jobs('', '')
    assert jobs[0]["location"] == "India"


def test_fetch_job_description_uses_cache_from_fetch_jobs(monkeypatch):
    get = Mock(return_value=list_response([
        raw_job("j1", "Engineer", description="<p>Build <b>scalable</b> systems.</p>"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', '')
    description, posting_date = fetcher.fetch_job_description(jobs[0]["application_url"])
    assert description == "Build scalable systems."
    assert posting_date == "2026-08-25"
    assert get.call_count == 1  # description served from cache, no extra call


def test_rate_limit_raised_on_429(monkeypatch):
    resp = Mock(status_code=429)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    assert get.call_count == 3


def test_rate_limit_raised_on_persistent_connection_failure(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    assert get.call_count == 3


def test_transient_failure_then_success_recovers(monkeypatch):
    good = list_response([raw_job("j1", "Engineer")])
    get = Mock(side_effect=[requests.ConnectionError('blip'), good])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 1
    assert get.call_count == 2


def test_cache_error_persists_across_calls(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    call_count_after_first = get.call_count

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    # No new HTTP calls -- the cached error is re-raised immediately.
    assert get.call_count == call_count_after_first
