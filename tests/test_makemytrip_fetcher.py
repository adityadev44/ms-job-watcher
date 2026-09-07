"""MakeMyTrip's custom careers-API cache, location dedup, date parsing, and
failure contracts (mocked at the HTTP seam — no real network calls)."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import makemytrip_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def _job(job_id, title, locations, created, post_on_careers_page=1):
    return {
        'job_id': job_id,
        'job_title': title,
        'location': locations,
        'location_country': 'India',
        'job_created_timestamp': created,
        'post_on_careers_page': post_on_careers_page,
    }


def _resp(json_data, status=200):
    r = Mock(status_code=status)
    r.json = Mock(return_value=json_data)
    r.raise_for_status = Mock() if status < 400 else Mock(side_effect=requests.HTTPError())
    return r


def test_fills_cache_and_slices(monkeypatch):
    jobs = [
        _job('1', 'Senior Software Engineer I (Backend)',
             ['Gurgaon, Haryana, India (Gurgaon_MMT)'], '08-10-2025 16:11:10'),
        _job('2', 'Product Analyst', ['Bangalore, Karnataka, India (Bangalore_MMT)'],
             '24-06-2026 11:57:36'),
        _job('3', 'Internal Only', ['Gurgaon, Haryana, India (Gurgaon_MMT)'],
             '01-01-2026 00:00:00', post_on_careers_page=0),
    ]
    get = Mock(return_value=_resp({'allJobs': jobs, 'businessUnits': [], 'locations': []}))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    result = fetcher.fetch_jobs('software engineer', 'India', num=1, start=0)
    assert len(result) == 1
    assert result[0]['id'] == '1'
    assert result[0]['location'] == 'Gurgaon, Haryana, India'
    assert result[0]['posting_date'] == '2025-10-08'
    assert result[0]['application_url'] == (
        'https://careers.makemytrip.com/prod/opportunity/1/'
        'senior-software-engineer-i-backend')

    # post_on_careers_page=0 job must be excluded
    all_jobs = fetcher.fetch_jobs('irrelevant', '', num=100, start=0)
    assert {j['id'] for j in all_jobs} == {'1', '2'}
    assert get.call_count == 1  # keyword ignored server-side, cache reused


def test_duplicate_locations_after_stripping_office_code_are_deduped(monkeypatch):
    jobs = [_job('1', 'Product Manager (Hotels)', [
        'Gurgaon, Haryana, India (Gurgaon_RBM)',
        'Bangalore, Karnataka, India (Bangalore_MMT)',
        'Gurgaon, Haryana, India (Gurgaon_MMT)',
    ], '05-08-2026 14:30:11')]
    get = Mock(return_value=_resp({'allJobs': jobs}))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    result = fetcher.fetch_jobs('', '', num=100)
    assert result[0]['location'] == 'Gurgaon, Haryana, India; Bangalore, Karnataka, India'


def test_missing_location_falls_back_to_country(monkeypatch):
    jobs = [_job('1', 'X', [], '05-08-2026 14:30:11')]
    get = Mock(return_value=_resp({'allJobs': jobs}))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    result = fetcher.fetch_jobs('', '', num=100)
    assert result[0]['location'] == 'India'


def test_unparseable_date_becomes_empty_string(monkeypatch):
    jobs = [_job('1', 'X', ['Mumbai, Maharashtra, India (Mumbai_MMT)'], 'garbage')]
    get = Mock(return_value=_resp({'allJobs': jobs}))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    result = fetcher.fetch_jobs('', '', num=100)
    assert result[0]['posting_date'] == ''


def test_missing_alljobs_key_never_becomes_silent_empty_success(monkeypatch):
    get = Mock(return_value=_resp({'businessUnits': []}))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '', num=100)
    assert get.call_count == 1


def test_429_raises_ratelimiterror(monkeypatch):
    get = Mock(return_value=_resp(None, status=429))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '', num=100)


def test_connection_failure_retries_then_raises(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '', num=100)
    assert get.call_count == 3


def test_fetch_job_description_double_unescapes_and_strips_html(monkeypatch):
    detail_json = {
        'status': 1,
        'data': {
            'job_decription': '&lt;p&gt;Build &amp;amp; ship &lt;b&gt;Java&lt;/b&gt; systems.&lt;/p&gt;',
            'job_created_timestamp': '08-10-2025 16:11:10',
        },
    }
    get = Mock(return_value=_resp(detail_json))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    desc, date = fetcher.fetch_job_description(
        'https://careers.makemytrip.com/prod/opportunity/a68e63fc6b5519/senior-software-engineer-i-backend')
    assert desc == 'Build & ship Java systems.'
    assert date == '2025-10-08'
    called_url = get.call_args[0][0]
    assert 'jobId=a68e63fc6b5519' in called_url
