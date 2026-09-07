"""ixigo's SmartRecruiters postings mapping, India-only filtering, and
failure contracts (mocked at the HTTP seam — no real network calls)."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import ixigo_fetcher as fetcher


@pytest.fixture(autouse=True)
def clear_desc_cache():
    fetcher._desc_cache.clear()


def _posting(job_id, name, city, country, released):
    return {
        'id': job_id,
        'name': name,
        'location': {'city': city, 'country': country},
        'releasedDate': released,
    }


def _resp(json_data, status=200):
    r = Mock(status_code=status)
    r.json = Mock(return_value=json_data)
    r.raise_for_status = Mock() if status < 400 else Mock(side_effect=requests.HTTPError())
    return r


def test_maps_postings_and_builds_application_url(monkeypatch):
    content = [
        _posting('1', 'Senior Software Engineer - Android', 'Gurugram', 'in',
                 '2026-09-04T06:48:55.902Z'),
        _posting('2', 'Business Development Executive', 'Gurgaon', 'in',
                 '2026-07-01T00:00:00.000Z'),
    ]
    get = Mock(return_value=_resp({'content': content, 'totalFound': 2}))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=20, start=0)
    assert len(jobs) == 2
    assert jobs[0]['id'] == '1'
    assert jobs[0]['location'] == 'Gurugram, India'
    assert jobs[0]['posting_date'] == '2026-09-04'
    assert jobs[0]['application_url'] == 'https://jobs.smartrecruiters.com/ixigo/1'

    params = get.call_args[1]['params']
    assert params['q'] == 'software engineer'
    assert params['country'] == 'in'


def test_non_india_postings_are_excluded(monkeypatch):
    content = [
        _posting('1', 'Local Role', 'Gurugram', 'in', '2026-01-01T00:00:00.000Z'),
        _posting('2', 'Overseas Role', 'Singapore', 'sg', '2026-01-01T00:00:00.000Z'),
    ]
    get = Mock(return_value=_resp({'content': content}))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    jobs = fetcher.fetch_jobs('', '', num=20)
    assert [j['id'] for j in jobs] == ['1']


def test_missing_city_falls_back_to_india(monkeypatch):
    content = [_posting('1', 'X', '', 'in', '2026-01-01T00:00:00.000Z')]
    get = Mock(return_value=_resp({'content': content}))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    jobs = fetcher.fetch_jobs('', '', num=20)
    assert jobs[0]['location'] == 'India'


def test_429_raises_ratelimiterror(monkeypatch):
    get = Mock(return_value=_resp(None, status=429))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '', num=20)


def test_search_connection_failure_retries_then_raises(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '', num=20)
    assert get.call_count == 3


def test_fetch_job_description_strips_html_and_excludes_company_boilerplate(monkeypatch):
    detail = {
        'jobAd': {'sections': {
            'companyDescription': {'text': '<p>Generic boilerplate about ixigo.</p>'},
            'jobDescription': {'text': '<p>Build <b>Android</b> apps.</p>'},
            'qualifications': {'text': '<ul><li>3-5 years experience</li></ul>'},
            'additionalInformation': {'text': ''},
        }},
        'releasedDate': '2026-09-04T06:48:55.902Z',
    }
    get = Mock(return_value=_resp(detail))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    desc, date = fetcher.fetch_job_description('https://jobs.smartrecruiters.com/ixigo/744000147427269-senior-software-engineer-android')
    assert 'boilerplate' not in desc
    assert 'Build Android apps.' in desc
    assert '3-5 years experience' in desc
    assert date == '2026-09-04'

    called_url = get.call_args[0][0]
    assert called_url.endswith('/744000147427269')

    # cached — a second call for the same URL makes no further request
    get.reset_mock()
    fetcher.fetch_job_description('https://jobs.smartrecruiters.com/ixigo/744000147427269-senior-software-engineer-android')
    assert get.call_count == 0
