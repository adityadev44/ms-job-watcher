"""L&T's PeopleStrong search-pagination, location-hierarchy, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import larsentoubro_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(fetcher, '_FIRST_PAGE_IDS', {})
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def api_response(jobs, total=None):
    return Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {
            "totalRecords": total if total is not None else len(jobs),
            "response": jobs,
        },
    )


def raw_job(req_id, title, hierarchy='India>Karnataka>Bengaluru', date='2026-09-05'):
    return {
        "requisitionId": req_id,
        "jobTitle": title,
        "locationHierarchyComplete": hierarchy,
        "jobPostedDate": date,
        "jobDetailUrl": f"https://larsentoubrocareers.peoplestrong.com/job/detail/LNT_D_{req_id}",
    }


def test_fetch_jobs_maps_fields_and_sends_search_string(monkeypatch):
    post = Mock(return_value=api_response([raw_job(1, 'Software Engineer')], total=1))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20, start=0)
    assert jobs == [{
        'id': '1',
        'title': 'Software Engineer',
        'location': 'Bengaluru, Karnataka, India',
        'posting_date': '2026-09-05',
        'application_url': 'https://larsentoubrocareers.peoplestrong.com/job/detail/LNT_D_1',
    }]
    _, kwargs = post.call_args
    assert kwargs['params'] == {'offset': 0, 'limit': 20, 'searchString': 'software engineer'}
    assert kwargs['json'] == fetcher._SEARCH_BODY


def test_fetch_jobs_omits_search_string_for_empty_keyword(monkeypatch):
    post = Mock(return_value=api_response([]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    fetcher.fetch_jobs('', '', num=20, start=0)
    _, kwargs = post.call_args
    assert 'searchString' not in kwargs['params']


def test_overseas_location_left_unmodified_for_is_india_job(monkeypatch):
    post = Mock(return_value=api_response([raw_job(2, 'Design Engineer', hierarchy='Oman>Sohar')]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    jobs = fetcher.fetch_jobs('engineer', '', num=20, start=0)
    assert jobs[0]['location'] == 'Sohar, Oman'
    assert 'india' not in jobs[0]['location'].lower()


def test_pagination_dedup_across_pages(monkeypatch):
    page1 = api_response([raw_job(1, 'Engineer A'), raw_job(2, 'Engineer B')])
    page2 = api_response([raw_job(3, 'Engineer C')])
    post = Mock(side_effect=[page1, page2])
    monkeypatch.setattr(fetcher.requests, 'post', post)

    first = fetcher.fetch_jobs('engineer', '', num=2, start=0)
    second = fetcher.fetch_jobs('engineer', '', num=2, start=2)
    assert [j['id'] for j in first] == ['1', '2']
    assert [j['id'] for j in second] == ['3']


def test_wraparound_guard_stops_replayed_first_page(monkeypatch):
    replay = api_response([raw_job(1, 'Engineer A'), raw_job(2, 'Engineer B')])
    post = Mock(side_effect=[replay, replay])
    monkeypatch.setattr(fetcher.requests, 'post', post)

    first = fetcher.fetch_jobs('engineer', '', num=2, start=0)
    second = fetcher.fetch_jobs('engineer', '', num=2, start=2)
    assert [j['id'] for j in first] == ['1', '2']
    assert second == []  # same ids replayed -> treated as wraparound, not real data


def test_different_keywords_track_independent_first_pages(monkeypatch):
    same_ids = api_response([raw_job(1, 'Engineer A')])
    post = Mock(return_value=same_ids)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    fetcher.fetch_jobs('engineer', '', num=1, start=0)
    # A different keyword's start=0 page must not be treated as a replay of
    # 'engineer's first page even though the ids happen to coincide.
    result = fetcher.fetch_jobs('developer', '', num=1, start=0)
    assert [j['id'] for j in result] == ['1']


@pytest.mark.parametrize('hierarchy,expected', [
    ('India>Karnataka>Bengaluru', 'Bengaluru, Karnataka, India'),
    ('Oman>Sohar', 'Sohar, Oman'),
    ('India', 'India'),
    ('', ''),
])
def test_normalise_location(hierarchy, expected):
    assert fetcher._normalise_location(hierarchy) == expected


@pytest.mark.parametrize('raw,expected', [
    ('2026-09-05', '2026-09-05'),
    ('2026-09-05 00:00:00.0', '2026-09-05'),
    ('', ''),
    ('garbage', ''),
])
def test_parse_datetime_to_date(raw, expected):
    assert fetcher._parse_datetime_to_date(raw) == expected


def test_search_429_raises_rate_limit_error(monkeypatch):
    resp = Mock(status_code=429)
    post = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'post', post)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', '', num=20, start=0)
    assert post.call_count == 3


def test_search_connection_failure_raises_rate_limit_error(monkeypatch):
    post = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', '', num=20, start=0)
    assert post.call_count == 3


def test_detail_extracts_description_and_date(monkeypatch):
    response = Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {
            "response": {
                "jobDescription": "<p>Build services with C# &amp; .NET.</p>",
                "CandidatePortalStartDate": "2026-09-05 00:00:00.0",
            }
        },
    )
    get = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, date = fetcher.fetch_job_description(
        'https://larsentoubrocareers.peoplestrong.com/job/detail/LNT_D_1848745'
    )
    assert description == 'Build services with C# & .NET.'
    assert date == '2026-09-05'
    _, kwargs = get.call_args
    assert kwargs['params']['part'] == fetcher._DETAIL_PARTS
    assert 'LNT_D_1848745' in get.call_args[0][0]


def test_detail_url_without_job_code_returns_empty(monkeypatch):
    get = Mock()
    monkeypatch.setattr(fetcher.requests, 'get', get)
    assert fetcher.fetch_job_description('https://example.com/not-a-job-url') == ('', '')
    get.assert_not_called()


def test_detail_request_failure_is_reported(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('unavailable'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description(
            'https://larsentoubrocareers.peoplestrong.com/job/detail/LNT_D_1'
        )
    assert get.call_count == 3
