"""TransUnion CIBIL's Workday CXS pagination, India-facet, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import transunioncibil_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def search_response(job_postings, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"total": len(job_postings), "jobPostings": job_postings},
    )


def raw_job(job_id, title, external_path=None, loc_text="Bengaluru",
            posted_on="Posted 3 Days Ago"):
    return {
        "title": title,
        "externalPath": external_path or f"/job/Bengaluru/{title.replace(' ', '-')}_{job_id}",
        "locationsText": loc_text,
        "postedOn": posted_on,
        "bulletFields": [job_id],
    }


def test_fetch_jobs_sends_india_facet_and_maps_fields(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("19041439", "Lead Developer, C# .NET & APIs", loc_text="Pune"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('.NET developer', 'India', num=20)
    assert len(jobs) == 1
    j = jobs[0]
    assert j['id'] == '19041439'
    assert j['title'] == 'Lead Developer, C# .NET & APIs'
    # Pune is kept (tagged India) so matcher.py's exclude_locations layer can
    # reject it downstream -- the fetcher itself must not filter it out.
    assert j['location'] == 'Pune, India'
    assert j['posting_date'] == (fetcher.date.today() - fetcher.timedelta(days=3)).strftime('%Y-%m-%d')
    assert j['application_url'] == (
        'https://transunion.wd5.myworkdayjobs.com/TransUnion'
        '/job/Bengaluru/Lead-Developer,-C#-.NET-&-APIs_19041439'
    )

    _, kwargs = post.call_args
    body = kwargs['json']
    assert body['appliedFacets'] == {'locationCountry': [fetcher._INDIA_COUNTRY_WID]}
    assert body['searchText'] == '.NET developer'


def test_ambiguous_multi_location_kept_and_tagged_india(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("1", "Senior Developer Software", loc_text="2 Locations"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs[0]['location'] == '2 Locations, India'


def test_location_normalization_keeps_genuine_india_text(monkeypatch):
    post = Mock(return_value=search_response([
        raw_job("1", "Engineer", loc_text="Mumbai - One World Center, India"),
    ]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs[0]['location'] == 'Mumbai - One World Center, India'


def test_job_without_title_or_id_is_skipped(monkeypatch):
    bad_no_title = raw_job("1", "Engineer")
    bad_no_title["title"] = ""
    bad_no_id = raw_job("2", "Engineer")
    bad_no_id["bulletFields"] = []
    bad_no_id["externalPath"] = ""
    good = raw_job("3", "Real Engineer")

    post = Mock(return_value=search_response([bad_no_title, bad_no_id, good]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert jobs[0]['id'] == '3'


def test_rate_limit_raised_after_persistent_429(monkeypatch):
    resp = Mock(status_code=429, raise_for_status=lambda: None)
    post = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert post.call_count == 3


def test_rate_limit_raised_after_persistent_connection_errors(monkeypatch):
    post = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert post.call_count == 3


def test_fetch_job_description_strips_html_and_returns_start_date(monkeypatch):
    get = Mock(return_value=Mock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"jobPostingInfo": {
            "jobDescription": "<p>Build with <b>LangChain</b> &amp; RAG.</p>",
            "startDate": "2026-08-25",
        }},
    ))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, posting_date = fetcher.fetch_job_description(
        'https://transunion.wd5.myworkdayjobs.com/TransUnion/job/Bengaluru/AI-Ops_32118'
    )
    assert description == 'Build with LangChain & RAG.'
    assert posting_date == '2026-08-25'

    args, _ = get.call_args
    assert args[0] == (
        'https://transunion.wd5.myworkdayjobs.com/wday/cxs/transunion/TransUnion'
        '/job/Bengaluru/AI-Ops_32118'
    )


def test_fetch_job_description_rate_limit_on_429(monkeypatch):
    resp = Mock(status_code=429, raise_for_status=lambda: None)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description(
            'https://transunion.wd5.myworkdayjobs.com/TransUnion/job/Bengaluru/X_1'
        )
    # A 429 raises immediately without retry (matches the Invesco-template
    # convention) -- only RequestException triggers the one-retry path.
    assert get.call_count == 1
