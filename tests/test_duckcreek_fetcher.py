"""Duck Creek's Workday CXS pagination, multi-location resolution, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import duckcreek_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def search_response(postings, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"total": 0, "jobPostings": postings},
    )


def detail_response(location='Mumbai, India', additional=None, description='<p>desc</p>',
                     start_date='2026-08-09', status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {
            "jobPostingInfo": {
                "location": location,
                "additionalLocations": additional or [],
                "jobDescription": description,
                "startDate": start_date,
                "postedOn": "Posted 30+ Days Ago",
            }
        },
    )


def posting(req_id, title, external_path=None, loc='Mumbai, India', posted_on='Posted Today'):
    return {
        "title": title,
        "externalPath": external_path or f"/job/Mumbai-India/{title.replace(' ', '-')}_{req_id}-1",
        "locationsText": loc,
        "postedOn": posted_on,
        "bulletFields": [req_id],
    }


def test_fetch_jobs_maps_fields_single_location(monkeypatch):
    post = Mock(return_value=search_response([
        posting('REQID54844', 'Principal Software Architect'),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert len(jobs) == 1
    job = jobs[0]
    assert job['id'] == 'REQID54844'
    assert job['title'] == 'Principal Software Architect'
    assert job['location'] == 'Mumbai, India'
    assert job['application_url'] == (
        'https://duckcreek.wd1.myworkdayjobs.com/duckcreekcareers'
        '/job/Mumbai-India/Principal-Software-Architect_REQID54844-1'
    )

    _, kwargs = post.call_args
    body = kwargs['json']
    assert body['appliedFacets'] == {}
    assert body['searchText'] == ''


def test_multi_location_posting_resolved_via_detail(monkeypatch):
    post = Mock(return_value=search_response([
        posting('REQID55124', 'Senior Business Analyst', loc='2 Locations'),
    ]))
    get = Mock(return_value=detail_response(
        location='Mumbai, India', additional=['Bengaluru, India'],
    ))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert len(jobs) == 1
    assert jobs[0]['location'] == 'Mumbai, India; Bengaluru, India'
    assert get.called


def test_non_india_location_is_skipped(monkeypatch):
    postings = [
        posting('REQID1', 'Software Engineer', loc='Mumbai, India'),
        posting('REQID2', 'Software Engineer', loc='Bay Area, CA'),
        posting('REQID3', 'Software Engineer', loc='United States - Indianapolis'),
    ]
    post = Mock(return_value=search_response(postings))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert [j['id'] for j in jobs] == ['REQID1']


def test_multi_location_resolved_to_non_india_is_dropped(monkeypatch):
    post = Mock(return_value=search_response([
        posting('REQID9', 'Sales Role', loc='2 Locations'),
    ]))
    get = Mock(return_value=detail_response(location='Paris', additional=['London']))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert jobs == []


def test_missing_title_or_job_id_is_dropped(monkeypatch):
    postings = [
        {"title": "", "externalPath": "/job/x/y_REQID9", "locationsText": "Mumbai, India",
         "postedOn": "Posted Today", "bulletFields": ["REQID9"]},
        {"title": "No ID Role", "externalPath": "/job/x/y", "locationsText": "Mumbai, India",
         "postedOn": "Posted Today", "bulletFields": []},
    ]
    post = Mock(return_value=search_response(postings))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert jobs == []


def test_limit_clamped_to_twenty(monkeypatch):
    post = Mock(return_value=search_response([]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    fetcher.fetch_jobs('', 'India', num=200, start=0)
    _, kwargs = post.call_args
    assert kwargs['json']['limit'] == 20


def test_posted_on_relative_dates_parsed(monkeypatch):
    import datetime
    today = datetime.date.today()

    postings = [
        posting('REQID1', 'Engineer A', posted_on='Posted Today'),
        posting('REQID2', 'Engineer B', posted_on='Posted 4 Days Ago'),
        posting('REQID3', 'Engineer C', posted_on='Posted 30+ Days Ago'),
    ]
    post = Mock(return_value=search_response(postings))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    dates = {j['id']: j['posting_date'] for j in jobs}
    assert dates['REQID1'] == today.strftime('%Y-%m-%d')
    assert dates['REQID2'] == (today - datetime.timedelta(days=4)).strftime('%Y-%m-%d')
    assert dates['REQID3'] == (today - datetime.timedelta(days=30)).strftime('%Y-%m-%d')


def test_fetch_jobs_retries_then_raises_rate_limit_error(monkeypatch):
    post = Mock(side_effect=requests.ConnectionError('boom'))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert post.call_count == 3


def test_fetch_jobs_429_retries_then_raises_rate_limit_error(monkeypatch):
    resp = Mock(status_code=429)
    post = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert post.call_count == 3


def test_fetch_job_description_strips_html_and_returns_start_date(monkeypatch):
    get = Mock(return_value=detail_response(description='<p>Build <b>AI</b> features with LangChain.</p>'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, posting_date = fetcher.fetch_job_description(
        'https://duckcreek.wd1.myworkdayjobs.com/duckcreekcareers'
        '/job/Mumbai-India/Principal-Software-Architect_REQID54844-1'
    )
    assert description == 'Build AI features with LangChain.'
    assert posting_date == '2026-08-09'

    called_url = get.call_args[0][0]
    assert called_url == (
        'https://duckcreek.wd1.myworkdayjobs.com/wday/cxs/duckcreek/duckcreekcareers'
        '/job/Mumbai-India/Principal-Software-Architect_REQID54844-1'
    )
