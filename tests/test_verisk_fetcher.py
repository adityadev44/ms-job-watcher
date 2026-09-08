"""Verisk's Oracle HCM CE search/detail contract, pagination, dedup, and India filtering."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import verisk_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def raw_req(job_id, title, location='Hyderabad, Telangana, India', posted='2026-09-01T00:00:00+00:00'):
    return {
        "Id": job_id,
        "Title": title,
        "PostedDate": posted,
        "PrimaryLocation": location,
    }


def search_response(req_list, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {
            "items": [{
                "TotalJobsCount": len(req_list),
                "requisitionList": req_list,
            }],
        },
    )


def detail_response(job, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"items": [job] if job is not None else []},
    )


def test_fetch_jobs_maps_fields_and_filters_india(monkeypatch):
    req_list = [
        raw_req('1', 'Sr. Software Engineer - .NET', location='Hyderabad, Telangana, India'),
        raw_req('2', 'Software Engineer', location='Krakow, Poland'),
        raw_req('3', 'Senior Architect', location='India'),
    ]
    get = Mock(return_value=search_response(req_list))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('engineer', 'India', num=25)

    assert [j['id'] for j in jobs] == ['1', '3']
    assert jobs[0]['title'] == 'Sr. Software Engineer - .NET'
    assert jobs[0]['location'] == 'Hyderabad, Telangana, India'
    assert jobs[0]['posting_date'] == '2026-09-01'
    assert jobs[0]['application_url'] == (
        'https://fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1'
    )


def test_fetch_jobs_dedups_repeated_ids(monkeypatch):
    req_list = [
        raw_req('1', 'Software Engineer'),
        raw_req('1', 'Software Engineer (dup)'),
        raw_req('2', 'Technical Lead'),
    ]
    get = Mock(return_value=search_response(req_list))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('engineer', 'India')
    assert [j['id'] for j in jobs] == ['1', '2']


def test_indianapolis_is_not_treated_as_india(monkeypatch):
    req_list = [
        raw_req('1', 'Software Engineer', location='Indianapolis, IN, United States'),
        raw_req('2', 'Software Engineer', location='Hyderabad, Telangana, India'),
    ]
    get = Mock(return_value=search_response(req_list))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('engineer', 'India')
    assert [j['id'] for j in jobs] == ['2']


def test_blank_keyword_falls_back_to_broad_term(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured['finder'] = params['finder']
        return search_response([raw_req('1', 'Software Engineer')])

    monkeypatch.setattr(fetcher.requests, 'get', fake_get)

    fetcher.fetch_jobs('', 'India')
    assert 'keyword=""' not in captured['finder']
    assert 'keyword="engineer"' in captured['finder']


def test_pagination_offsets_do_not_overlap(monkeypatch):
    page1 = [raw_req(str(i), f'Engineer {i}') for i in range(5)]
    page2 = [raw_req(str(i), f'Engineer {i}') for i in range(5, 8)]

    def fake_get(url, headers=None, params=None, timeout=None):
        if 'offset=0' in params['finder']:
            return search_response(page1)
        return search_response(page2)

    monkeypatch.setattr(fetcher.requests, 'get', fake_get)

    jobs1 = fetcher.fetch_jobs('engineer', 'India', num=5, start=0)
    jobs2 = fetcher.fetch_jobs('engineer', 'India', num=5, start=5)

    assert [j['id'] for j in jobs1] == ['0', '1', '2', '3', '4']
    assert [j['id'] for j in jobs2] == ['5', '6', '7']


def test_empty_requisition_list_returns_empty(monkeypatch):
    get = Mock(return_value=search_response([]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    assert fetcher.fetch_jobs('engineer', 'India') == []


def test_fetch_jobs_raises_rate_limit_on_429(monkeypatch):
    resp = Mock(status_code=429)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', 'India')


def test_fetch_jobs_raises_rate_limit_after_persistent_failures(monkeypatch):
    get = Mock(side_effect=requests.exceptions.ConnectionError('boom'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', 'India')
    assert get.call_count == 3


def test_fetch_jobs_retries_then_succeeds(monkeypatch):
    ok = search_response([raw_req('1', 'Software Engineer')])
    get = Mock(side_effect=[requests.exceptions.ConnectionError('boom'), ok])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('engineer', 'India')
    assert [j['id'] for j in jobs] == ['1']
    assert get.call_count == 2


def test_fetch_job_description_combines_and_strips_html(monkeypatch):
    job = {
        "ExternalDescriptionStr": "<p>Build <b>things</b>.</p>",
        "ExternalResponsibilitiesStr": "<ul><li>Own delivery</li></ul>",
        "ExternalQualificationsStr": "<p>5+ years experience</p>",
        "ExternalPostedStartDate": "2026-09-07T06:20:49+00:00",
    }
    get = Mock(return_value=detail_response(job))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    desc, date = fetcher.fetch_job_description(
        'https://fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/4255'
    )
    assert desc == 'Build things . Own delivery 5+ years experience'
    assert date == '2026-09-07'


def test_fetch_job_description_missing_item_returns_empty(monkeypatch):
    get = Mock(return_value=detail_response(None))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    desc, date = fetcher.fetch_job_description('.../job/999')
    assert (desc, date) == ('', '')


def test_fetch_job_description_persistent_failure_returns_empty(monkeypatch):
    get = Mock(side_effect=requests.exceptions.ConnectionError('boom'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    desc, date = fetcher.fetch_job_description('.../job/999')
    assert (desc, date) == ('', '')
