"""Vedanta's Darwinbox "candidate"-SPA cache-fill, location, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import vedanta_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_detail_cache', {})
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher, '_FIRST_PAGE_IDS', None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def list_response(jobs):
    return Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"status": "success", "message": {"jobscount": len(jobs), "jobs": jobs}},
    )


def raw_job(job_id, title, loc='Jharsuguda, Odisha, India', tips=None, posted=1788287400):
    return {
        "id": job_id,
        "title": title,
        "officelocation_show_arr": loc,
        "tool_tip_locations": tips or [loc],
        "job_posting_on": posted,
    }


def test_fill_cache_paginates_dedups_and_maps_fields(monkeypatch):
    page1 = list_response([raw_job(str(i), 'Engineer') for i in range(50)])
    page2 = list_response([raw_job(str(i), 'Engineer') for i in range(50, 53)])
    page3 = list_response([])
    get = Mock(side_effect=[page1, page2, page3])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('software', 'India', num=100)
    assert len(jobs) == 53
    assert jobs[0]['id'] == '0'
    assert jobs[0]['title'] == 'Engineer'
    assert jobs[0]['location'] == 'Jharsuguda, Odisha, India'
    assert jobs[0]['posting_date'] == '2026-09-01'
    assert jobs[0]['application_url'] == 'https://vhr.darwinbox.in/ms/candidate/careers/job/0'
    assert get.call_count == 3

    # Cache reused -- no further HTTP calls.
    assert fetcher.fetch_jobs('different', '', start=50, num=20) == jobs[50:]
    assert get.call_count == 3


def test_multiple_locations_falls_back_to_tooltip_list():
    job = raw_job('1', 'Head Exploration', loc='Multiple locations',
                   tips=['Bhadrak, Odisha, India ', 'Ostapal, Jajapur, Odisha, India '])
    assert fetcher._location_from_job(job) == 'Bhadrak, Odisha, India; Ostapal, Jajapur, Odisha, India'


def test_overseas_location_left_unmodified():
    job = raw_job('2', 'Plant Operator', loc='Aggeneys, Northern Cape, South Africa',
                   tips=['Aggeneys, Northern Cape, South Africa'])
    loc = fetcher._location_from_job(job)
    assert 'india' not in loc.lower()


def test_stops_on_replayed_page(monkeypatch):
    looping_page = list_response([raw_job('1', 'Engineer'), raw_job('2', 'Engineer')])
    get = Mock(return_value=looping_page)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 2
    assert get.call_count == 2


@pytest.mark.parametrize('ts,expected', [
    (1788287400, '2026-09-01'),
    (None, ''),
    (0, ''),
    ('garbage', ''),
])
def test_epoch_to_date(ts, expected):
    assert fetcher._epoch_to_date(ts) == expected


def test_failed_cache_never_becomes_silent_empty_success(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '')
    # First call retries 3x inside _get_json; second call is served from
    # the cached error with no further HTTP calls.
    assert get.call_count == 3


def test_detail_extracts_description_and_date(monkeypatch):
    response = Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"status": "success", "message": {"job": [{
            "id": "1",
            "jd": "&lt;p&gt;Build services with C# &amp;amp; .NET.&lt;/p&gt;",
            "posted_on": 1788287400,
        }]}},
    )
    get = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, date = fetcher.fetch_job_description(
        'https://vhr.darwinbox.in/ms/candidate/careers/job/1'
    )
    assert description == 'Build services with C# & .NET.'
    assert date == '2026-09-01'
    # Second call for the same job is served from the detail cache.
    fetcher.fetch_job_description('https://vhr.darwinbox.in/ms/candidate/careers/job/1')
    assert get.call_count == 1


def test_detail_request_failure_is_reported(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('unavailable'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description('https://vhr.darwinbox.in/ms/candidate/careers/job/1')
    assert get.call_count == 3
