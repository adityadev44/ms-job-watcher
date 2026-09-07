"""JSW Group's TurboHire cache-fill, location, token-refresh, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import jsw_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher, '_bearer_token', None)
    monkeypatch.setattr(fetcher, '_token_expiry', 0.0)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def token_response():
    return Mock(status_code=200, raise_for_status=lambda: None,
                json=lambda: {"access_token": "tok123", "expires_in": 3600})


def filteredjobs_response(jobs):
    return Mock(status_code=200, raise_for_status=lambda: None,
                json=lambda: {"Total": len(jobs), "Result": jobs})


def raw_job(job_id, title, address='Chhatrapati Sambhajinagar, Maharashtra, India',
            posted='2026-09-04T06:30:04.353873Z'):
    import json as _json
    return {
        "JobId": job_id,
        "JobIdObfuscated": f"tok_{job_id}%2Fsuffix",
        "JobTitle": title,
        "Location": _json.dumps([{"Address": address, "PlaceId": None}]),
        "PublishedDate": posted,
        "JobDescV2": "<p>truncated...</p>",
    }


def test_fill_cache_maps_fields_and_dedups(monkeypatch):
    get = Mock(return_value=token_response())
    post = Mock(return_value=filteredjobs_response([raw_job('a1', 'Design Engineer'), raw_job('a1', 'Duplicate')]))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=100)
    assert len(jobs) == 1
    assert jobs[0] == {
        'id': 'a1',
        'title': 'Design Engineer',
        'location': 'Chhatrapati Sambhajinagar, Maharashtra, India',
        'posting_date': '2026-09-04',
        'application_url': 'https://jswgroup.turbohire.co/job/publicjobs/tok_a1%2Fsuffix',
    }
    _, kwargs = post.call_args
    assert kwargs['params'] == {'orgId': fetcher._ORG_ID, 'pageType': 0}

    # Cache reused for a second, different keyword -- no further HTTP calls.
    fetcher.fetch_jobs('different', '', start=0, num=100)
    assert post.call_count == 1
    assert get.call_count == 1


def test_location_without_india_gets_it_appended(monkeypatch):
    get = Mock(return_value=token_response())
    post = Mock(return_value=filteredjobs_response([raw_job('a2', 'Some Role', address='Bengaluru, Karnataka')]))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    monkeypatch.setattr(fetcher.requests, 'post', post)
    jobs = fetcher.fetch_jobs('', '', num=100)
    assert jobs[0]['location'] == 'Bengaluru, Karnataka, India'


def test_location_missing_falls_back_to_india(monkeypatch):
    get = Mock(return_value=token_response())
    job = raw_job('a3', 'Some Role')
    job['Location'] = '[]'
    post = Mock(return_value=filteredjobs_response([job]))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    monkeypatch.setattr(fetcher.requests, 'post', post)
    jobs = fetcher.fetch_jobs('', '', num=100)
    assert jobs[0]['location'] == 'India'


def test_token_401_triggers_single_refresh_then_succeeds(monkeypatch):
    get = Mock(return_value=token_response())
    unauthorized = Mock(status_code=401)
    ok = filteredjobs_response([raw_job('a4', 'Engineer')])
    post = Mock(side_effect=[unauthorized, ok])
    monkeypatch.setattr(fetcher.requests, 'get', get)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', '', num=100)
    assert len(jobs) == 1
    assert post.call_count == 2
    assert get.call_count == 2  # token re-fetched after the 401


def test_token_fetch_failure_raises_rate_limit_error(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '', num=100)
    assert get.call_count == 3


def test_filteredjobs_connection_failure_raises_rate_limit_error(monkeypatch):
    get = Mock(return_value=token_response())
    post = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '', num=100)
    # First fetch_jobs call retries 3x; second is served from the cached
    # error (_cache_error already set) with no further HTTP calls.
    assert post.call_count == 3


def test_description_fetches_full_untruncated_text(monkeypatch):
    get = Mock(return_value=token_response())
    detail = Mock(status_code=200, raise_for_status=lambda: None, json=lambda: {
        "JobDescriptionV2": "<p>Build services with C# &amp; .NET.</p>",
        "PublishedDate": "2026-09-04T06:30:04.353873Z",
    })
    monkeypatch.setattr(fetcher.requests, 'get', Mock(side_effect=[token_response(), detail]))

    description, date = fetcher.fetch_job_description(
        'https://jswgroup.turbohire.co/job/publicjobs/tok_a1%2Fsuffix'
    )
    assert description == 'Build services with C# & .NET.'
    assert date == '2026-09-04'


def test_description_closed_posting_returns_empty_without_raising(monkeypatch):
    not_found = Mock(status_code=404)
    monkeypatch.setattr(fetcher.requests, 'get', Mock(side_effect=[token_response(), not_found]))
    result = fetcher.fetch_job_description('https://jswgroup.turbohire.co/job/publicjobs/tok_gone')
    assert result == ('', '')


def test_description_empty_url_returns_empty(monkeypatch):
    get = Mock()
    monkeypatch.setattr(fetcher.requests, 'get', get)
    assert fetcher.fetch_job_description('') == ('', '')
    get.assert_not_called()
