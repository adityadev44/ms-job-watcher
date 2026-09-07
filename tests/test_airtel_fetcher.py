"""Airtel's Darwinbox candidatev2 cache-fill, location-join, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import airtel_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_desc_cache', {})
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher, '_FIRST_PAGE_IDS', None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def api_response(jobs, job_counts=None, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {
            "status": "success",
            "job_counts": job_counts if job_counts is not None else len(jobs),
            "data": jobs,
        },
    )


def raw_job(job_id, title, tips=None, office_loc='Gurgaon, Haryana, India (BAL_1)',
            country='India', posted=1788460200, jd='<p>Build things.</p>'):
    return {
        "id": job_id,
        "title": title,
        "officelocation_show_arr": office_loc,
        "tool_tip_locations": tips,
        "country": country,
        "posted_on": posted,
        "jd": jd,
    }


def test_fill_cache_paginates_dedups_and_maps_fields(monkeypatch):
    page1 = api_response([raw_job(str(i), 'Engineer') for i in range(50)], job_counts=53)
    page2 = api_response([raw_job(str(i), 'Engineer') for i in range(50, 53)], job_counts=53)
    post = Mock(side_effect=[page1, page2])
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('software', 'India', num=100)
    assert len(jobs) == 53
    assert jobs[0]['id'] == '0'
    assert jobs[0]['title'] == 'Engineer'
    assert jobs[0]['location'] == 'Gurgaon, Haryana, India'
    assert jobs[0]['posting_date'] == '2026-09-03'
    assert jobs[0]['application_url'] == (
        'https://airtel.darwinbox.in/ms/candidatev2/main/careers/jobDetails/0?from=all'
    )
    assert post.call_count == 2

    # Cache reused for a second, different keyword -- no further HTTP calls.
    assert fetcher.fetch_jobs('different', '', start=50, num=20) == jobs[50:]
    assert post.call_count == 2


def test_multi_branch_tooltips_are_joined():
    job = raw_job('1', 'Territory Manager',
                   tips=['Chennai, Tamil Nadu, India', 'Coimbatore, Tamil Nadu, India'])
    assert fetcher._location_from_job(job) == 'Chennai, Tamil Nadu, India; Coimbatore, Tamil Nadu, India'


def test_office_location_code_suffix_is_stripped():
    job = raw_job('1', 'Circle Partnership', tips=None,
                   office_loc='Vijayawada, Andhra Pradesh, India (APBL_300000367251403)')
    assert fetcher._location_from_job(job) == 'Vijayawada, Andhra Pradesh, India'


def test_falls_back_to_country_then_india():
    assert fetcher._location_from_job({"tool_tip_locations": [], "officelocation_show_arr": "",
                                        "country": "India"}) == 'India'
    assert fetcher._location_from_job({}) == 'India'


def test_stops_on_replayed_page(monkeypatch):
    looping = api_response([raw_job('1', 'Engineer'), raw_job('2', 'Engineer')], job_counts=99)
    post = Mock(return_value=looping)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 2
    assert post.call_count == 2


def test_stops_when_collected_matches_job_counts(monkeypatch):
    page1 = api_response([raw_job('1', 'Engineer'), raw_job('2', 'Engineer')], job_counts=2)
    post = Mock(return_value=page1)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 2
    assert post.call_count == 1


def test_keyword_and_location_never_sent_in_body(monkeypatch):
    post = Mock(return_value=api_response([]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    fetcher.fetch_jobs('anything', 'India', num=10, start=0)
    _, kwargs = post.call_args
    body = kwargs['json']
    assert set(body.keys()) == {'companyId', 'page', 'sort_option', 'limit'}


def test_failed_cache_never_becomes_silent_empty_success(monkeypatch):
    post = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '')
    # First call retries 3x inside _call_api; second call served from the
    # cached error with no further HTTP calls.
    assert post.call_count == 3


def test_description_served_from_inline_cache(monkeypatch):
    post = Mock(return_value=api_response(
        [raw_job('1', 'Engineer', jd='<p>Build services with C# &amp; .NET.</p>')], job_counts=1
    ))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    description, date = fetcher.fetch_job_description(
        'https://airtel.darwinbox.in/ms/candidatev2/main/careers/jobDetails/1?from=all'
    )
    assert description == 'Build services with C# & .NET.'
    assert date == '2026-09-03'
    assert post.call_count == 1  # only the cache-fill call, no extra detail request


def test_description_unknown_job_id_returns_empty(monkeypatch):
    post = Mock(return_value=api_response([], job_counts=0))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    result = fetcher.fetch_job_description(
        'https://airtel.darwinbox.in/ms/candidatev2/main/careers/jobDetails/unknown?from=all'
    )
    assert result == ('', '')
