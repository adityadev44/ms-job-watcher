"""Aditya Birla Group's careers.adityabirla.com wrapper: search, soft-401,
inline-description cache, location cleanup, and pagination-guard contracts.
"""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import adityabirla_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(fetcher, '_FIRST_PAGE_IDS', {})
    monkeypatch.setattr(fetcher, '_description_cache', {})
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def api_response(jobs, status=200, total=None):
    return Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {
            "status": status,
            "count": len(jobs),
            "totalJobs": total if total is not None else len(jobs),
            "data": jobs,
        },
    )


def raw_job(job_id, title, hierarchy='India>Karnataka>Bengaluru', date='2026-09-05T00:00:00.000Z',
            description='<p>Build things.</p>'):
    return {
        "id": job_id,
        "jobTitle": title,
        "jobCode": f"ABG{job_id}",
        "locationHierarchyComplete": hierarchy,
        "jobPostedDate": date,
        "jobDetailUrl": f"https://abgcareers.peoplestrong.com/job/detail/ABG{job_id}",
        "jobDescription": description,
    }


def test_fetch_jobs_maps_fields_sends_search_string_and_caches_description(monkeypatch):
    get = Mock(return_value=api_response([raw_job(1, 'Software Engineer')]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20, start=0)
    assert jobs == [{
        'id': '1',
        'title': 'Software Engineer',
        'location': 'Bengaluru, Karnataka, India',
        'posting_date': '2026-09-05',
        'application_url': 'https://abgcareers.peoplestrong.com/job/detail/ABG1',
    }]
    _, kwargs = get.call_args
    assert kwargs['params'] == {'sortBy': 'new', 'offset': 0, 'limit': 20, 'searchString': 'software engineer'}
    assert fetcher._description_cache['https://abgcareers.peoplestrong.com/job/detail/ABG1'] == (
        'Build things.', '2026-09-05'
    )


def test_fetch_jobs_omits_search_string_for_empty_keyword(monkeypatch):
    get = Mock(return_value=api_response([]))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    fetcher.fetch_jobs('', '', num=20, start=0)
    _, kwargs = get.call_args
    assert 'searchString' not in kwargs['params']


def test_soft_401_json_body_raises_rate_limit_error_despite_http_200(monkeypatch):
    resp = api_response([], status=401)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', '', num=20, start=0)


def test_overseas_location_left_unmodified_for_is_india_job(monkeypatch):
    get = Mock(return_value=api_response([raw_job(2, 'Design Engineer', hierarchy='Oman>Sohar')]))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    jobs = fetcher.fetch_jobs('engineer', '', num=20, start=0)
    assert jobs[0]['location'] == 'Sohar, Oman'
    assert 'india' not in jobs[0]['location'].lower()


def test_redundant_state_suffix_is_trimmed():
    assert fetcher._normalise_location('India>Madhya Pradesh>Indore, Madhya Pradesh') == (
        'Indore, Madhya Pradesh, India'
    )


@pytest.mark.parametrize('hierarchy,expected', [
    ('India>Karnataka>Bengaluru', 'Bengaluru, Karnataka, India'),
    ('India', 'India'),
    ('', ''),
])
def test_normalise_location(hierarchy, expected):
    assert fetcher._normalise_location(hierarchy) == expected


@pytest.mark.parametrize('raw,expected', [
    ('2026-09-06T00:00:00.000Z', '2026-09-06'),
    ('2026-09-06 00:00:00.0', '2026-09-06'),
    ('', ''),
    ('garbage', ''),
])
def test_parse_iso_date(raw, expected):
    assert fetcher._parse_iso_date(raw) == expected


def test_pagination_dedup_across_pages(monkeypatch):
    page1 = api_response([raw_job(1, 'Engineer A'), raw_job(2, 'Engineer B')])
    page2 = api_response([raw_job(3, 'Engineer C')])
    get = Mock(side_effect=[page1, page2])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    first = fetcher.fetch_jobs('engineer', '', num=2, start=0)
    second = fetcher.fetch_jobs('engineer', '', num=2, start=2)
    assert [j['id'] for j in first] == ['1', '2']
    assert [j['id'] for j in second] == ['3']


def test_wraparound_guard_stops_replayed_first_page(monkeypatch):
    replay = api_response([raw_job(1, 'Engineer A'), raw_job(2, 'Engineer B')])
    get = Mock(side_effect=[replay, replay])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    first = fetcher.fetch_jobs('engineer', '', num=2, start=0)
    second = fetcher.fetch_jobs('engineer', '', num=2, start=2)
    assert [j['id'] for j in first] == ['1', '2']
    assert second == []


def test_search_429_raises_rate_limit_error(monkeypatch):
    resp = Mock(status_code=429)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', '', num=20, start=0)
    assert get.call_count == 3


def test_description_served_from_cache_without_extra_request(monkeypatch):
    fetcher._description_cache['https://abgcareers.peoplestrong.com/job/detail/ABG1'] = (
        'Cached description.', '2026-09-05'
    )
    get = Mock()
    monkeypatch.setattr(fetcher.requests, 'get', get)
    result = fetcher.fetch_job_description('https://abgcareers.peoplestrong.com/job/detail/ABG1')
    assert result == ('Cached description.', '2026-09-05')
    get.assert_not_called()


def test_description_falls_back_to_peoplestrong_detail_api_on_cache_miss(monkeypatch):
    response = Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"response": {
            "jobDescription": "<p>Build services with C# &amp; .NET.</p>",
            "CandidatePortalStartDate": "2026-09-05 00:00:00.0",
        }},
    )
    get = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, date = fetcher.fetch_job_description(
        'https://abgcareers.peoplestrong.com/job/detail/ABG104609'
    )
    assert description == 'Build services with C# & .NET.'
    assert date == '2026-09-05'
    _, kwargs = get.call_args
    assert kwargs['params']['part'] == fetcher._PS_DETAIL_PARTS
    assert 'ABG104609' in get.call_args[0][0]


def test_description_url_without_job_code_returns_empty(monkeypatch):
    get = Mock()
    monkeypatch.setattr(fetcher.requests, 'get', get)
    assert fetcher.fetch_job_description('https://example.com/not-a-job-url') == ('', '')
    get.assert_not_called()


def test_description_request_failure_is_reported(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('unavailable'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description('https://abgcareers.peoplestrong.com/job/detail/ABG1')
    assert get.call_count == 3
