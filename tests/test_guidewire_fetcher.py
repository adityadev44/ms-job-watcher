"""Guidewire's Workday CXS pagination, ID/location mapping, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import guidewire_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def search_response(postings, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"total": 0, "jobPostings": postings},
    )


def posting(job_id, title, external_path=None, loc='India - Bangalore', posted_on='Posted Today'):
    return {
        "title": title,
        "externalPath": external_path or f"/job/India---Bangalore/{title.replace(' ', '-')}_{job_id}",
        "locationsText": loc,
        "postedOn": posted_on,
        "bulletFields": [job_id],
    }


def test_fetch_jobs_maps_fields_and_applies_india_facet(monkeypatch):
    post = Mock(return_value=search_response([
        posting('JR_14950', 'AI Engineer'),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20, start=0)
    assert len(jobs) == 1
    job = jobs[0]
    assert job['id'] == 'JR_14950'
    assert job['title'] == 'AI Engineer'
    assert job['location'] == 'India - Bangalore'
    assert job['posting_date'] == __import__('datetime').date.today().strftime('%Y-%m-%d')
    assert job['application_url'] == (
        'https://wd5.myworkdaysite.com/recruiting/guidewire/external'
        '/job/India---Bangalore/AI-Engineer_JR_14950'
    )

    _, kwargs = post.call_args
    body = kwargs['json']
    assert body['appliedFacets'] == {'locationCountry': [fetcher._INDIA_WID]}
    assert body['searchText'] == 'software engineer'


def test_fetch_jobs_paginates_across_multiple_pages(monkeypatch):
    page1 = search_response([posting(f'JR_{i}', 'Software Engineer III') for i in range(20)])
    page2 = search_response([posting(f'JR_{i}', 'Software Engineer III') for i in range(20, 24)])
    post = Mock(side_effect=[page1, page2])
    monkeypatch.setattr(fetcher.requests, 'post', post)

    first = fetcher.fetch_jobs('', 'India', num=20, start=0)
    second = fetcher.fetch_jobs('', 'India', num=20, start=20)

    assert len(first) == 20
    assert len(second) == 4
    assert post.call_count == 2
    offsets = [c.kwargs['json']['offset'] for c in post.call_args_list]
    assert offsets == [0, 20]


def test_limit_clamped_to_twenty(monkeypatch):
    post = Mock(return_value=search_response([]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    fetcher.fetch_jobs('', 'India', num=200, start=0)
    _, kwargs = post.call_args
    assert kwargs['json']['limit'] == 20


def test_dedup_relies_on_stable_job_id_across_calls(monkeypatch):
    same_page = search_response([posting('JR_15116', 'Staff Software Engineer - Cloud Platform')])
    post = Mock(return_value=same_page)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    first = fetcher.fetch_jobs('cloud', 'India')
    second = fetcher.fetch_jobs('platform', 'India')
    assert first[0]['id'] == second[0]['id'] == 'JR_15116'


def test_job_id_falls_back_to_external_path_suffix(monkeypatch):
    p = posting('JR_15138', 'Staff Software Engineer', external_path=(
        '/job/India---Bangalore/Staff-Software-Engineer_JR_15138-1'
    ))
    p['bulletFields'] = []  # force fallback path
    post = Mock(return_value=search_response([p]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs[0]['id'] == 'JR_15138-1'


def test_non_india_location_is_skipped_as_safety_net(monkeypatch):
    postings = [
        posting('JR_1', 'Software Engineer', loc='India - Bangalore'),
        posting('JR_2', 'Software Engineer', loc='United States - Remote'),
        # Word-boundary guard: never treat "Indianapolis" as India.
        posting('JR_3', 'Software Engineer', loc='United States - Indianapolis'),
    ]
    post = Mock(return_value=search_response(postings))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert [j['id'] for j in jobs] == ['JR_1']


def test_missing_title_or_job_id_is_dropped(monkeypatch):
    postings = [
        {"title": "", "externalPath": "/job/x/y_JR_9", "locationsText": "India - Bangalore",
         "postedOn": "Posted Today", "bulletFields": ["JR_9"]},
        {"title": "No ID Role", "externalPath": "/job/x/y", "locationsText": "India - Bangalore",
         "postedOn": "Posted Today", "bulletFields": []},
    ]
    post = Mock(return_value=search_response(postings))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs == []


def test_posted_on_relative_dates_parsed(monkeypatch):
    import datetime
    today = datetime.date.today()

    postings = [
        posting('JR_1', 'Engineer A', posted_on='Posted Today'),
        posting('JR_2', 'Engineer B', posted_on='Posted 4 Days Ago'),
        posting('JR_3', 'Engineer C', posted_on='Posted 30+ Days Ago'),
    ]
    post = Mock(return_value=search_response(postings))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    dates = {j['id']: j['posting_date'] for j in jobs}
    assert dates['JR_1'] == today.strftime('%Y-%m-%d')
    assert dates['JR_2'] == (today - datetime.timedelta(days=4)).strftime('%Y-%m-%d')
    assert dates['JR_3'] == (today - datetime.timedelta(days=30)).strftime('%Y-%m-%d')


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


def detail_response(description, start_date='2026-08-09', status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {
            "jobPostingInfo": {
                "jobDescription": description,
                "startDate": start_date,
                "postedOn": "Posted 30+ Days Ago",
            }
        },
    )


def test_fetch_job_description_strips_html_and_returns_start_date(monkeypatch):
    get = Mock(return_value=detail_response('<p>Build <b>AI</b> features with LangChain.</p>'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, posting_date = fetcher.fetch_job_description(
        'https://wd5.myworkdaysite.com/recruiting/guidewire/external'
        '/job/India---Bangalore/AI-Engineer_JR_14950'
    )
    assert description == 'Build AI features with LangChain.'
    assert posting_date == '2026-08-09'

    called_url = get.call_args[0][0]
    assert called_url == (
        'https://guidewire.wd5.myworkdayjobs.com/wday/cxs/guidewire/External'
        '/job/India---Bangalore/AI-Engineer_JR_14950'
    )


def test_fetch_job_description_falls_back_to_posted_on_when_start_date_missing(monkeypatch):
    get = Mock(return_value=detail_response('<p>JD text.</p>', start_date=''))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    _, posting_date = fetcher.fetch_job_description(
        'https://wd5.myworkdaysite.com/recruiting/guidewire/external/job/x/y_JR_1'
    )
    assert posting_date == (
        __import__('datetime').date.today() - __import__('datetime').timedelta(days=30)
    ).strftime('%Y-%m-%d')


def test_fetch_job_description_unrecognized_url_returns_empty(monkeypatch):
    get = Mock(return_value=detail_response('<p>irrelevant</p>'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    result = fetcher.fetch_job_description('https://example.com/not-a-job-url')
    assert result == ('', '')
    get.assert_not_called()


def test_fetch_job_description_retries_then_raises_rate_limit_error(monkeypatch):
    get = Mock(side_effect=requests.Timeout('slow'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description(
            'https://wd5.myworkdaysite.com/recruiting/guidewire/external/job/x/y_JR_1'
        )
    assert get.call_count == 3
