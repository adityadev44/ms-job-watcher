"""ITC's Zoho Recruit Career Site: embedded-JSON extraction, inline-description
cache, location fallback, and failure contracts."""
from pathlib import Path
import sys
import json
import html as html_mod
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import itc_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_desc_cache', {})
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def raw_job(job_id, title, city='Kolkata', state='West Bengal', country='India',
            date='2026-07-09', desc='Full plain-text description already complete.'):
    return {
        "id": job_id,
        "Posting_Title": title,
        "Job_Opening_Name": title,
        "City": city,
        "State": state,
        "Country": country,
        "Date_Opened": date,
        "Job_Description": desc,
    }


def list_html(jobs):
    # Real ITC/Zoho markup places value="..." BEFORE id="jobs" -- the
    # extractor walks backward from the id marker to find it.
    payload = html_mod.escape(json.dumps(jobs), quote=True)
    return f'<html><body><input type="hidden" value="{payload}" id="jobs"></body></html>'


def mock_response(text):
    return Mock(status_code=200, text=text, raise_for_status=lambda: None)


def test_fill_cache_maps_fields_and_caches_inline_description(monkeypatch):
    html = list_html([raw_job('1', 'Software Engineer')])
    get = Mock(return_value=mock_response(html))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('anything', 'India', num=20, start=0)
    assert jobs == [{
        'id': '1',
        'title': 'Software Engineer',
        'location': 'Kolkata, West Bengal, India',
        'posting_date': '2026-07-09',
        'application_url': 'https://recruitment.itcportal.com/jobs/Careers/1',
    }]
    assert fetcher._desc_cache['1'] == 'Full plain-text description already complete.'
    assert get.call_count == 1

    # Second call with a different keyword reuses the cache -- no further HTTP calls.
    fetcher.fetch_jobs('different', '', start=0, num=20)
    assert get.call_count == 1


def test_query_params_never_sent_to_list_url(monkeypatch):
    get = Mock(return_value=mock_response(list_html([])))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    fetcher.fetch_jobs('keyword-that-should-be-ignored', 'India', num=20, start=0)
    args, kwargs = get.call_args
    assert args[0] == fetcher._LIST_URL
    assert 'params' not in kwargs


@pytest.mark.parametrize('job,expected', [
    ({'City': 'Bangalore', 'State': 'Karnataka', 'Country': 'India'}, 'Bangalore, Karnataka, India'),
    ({'City': None, 'State': None, 'Country': None}, 'India'),
    ({'City': 'Mumbai', 'State': None, 'Country': None}, 'Mumbai, India'),
    ({'City': 'Sohar', 'State': None, 'Country': 'Oman'}, 'Sohar, Oman'),
])
def test_location_from_job(job, expected):
    assert fetcher._location_from_job(job) == expected


def test_description_served_from_cache_without_extra_request(monkeypatch):
    html = list_html([raw_job('1', 'Engineer', desc='Complete description text.')])
    get = Mock(return_value=mock_response(html))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    fetcher.fetch_jobs('', '', num=20, start=0)
    description, date = fetcher.fetch_job_description(
        'https://recruitment.itcportal.com/jobs/Careers/1'
    )
    assert description == 'Complete description text.'
    assert date == '2026-07-09'
    assert get.call_count == 1  # only the list fetch, no separate detail call


def test_description_falls_back_to_detail_page_on_cache_miss(monkeypatch):
    detail_job = raw_job('2', 'Engineer', desc='<p>Detail page HTML description.</p>')
    detail_html = "var jobs = JSON.parse('" + json.dumps([detail_job]).replace('"', '\\x22').replace("'", "\\x27") + "');"
    get = Mock(return_value=mock_response(detail_html))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, date = fetcher.fetch_job_description(
        'https://recruitment.itcportal.com/jobs/Careers/2'
    )
    assert description == 'Detail page HTML description.'
    assert date == '2026-07-09'


def test_description_empty_url_raises(monkeypatch):
    get = Mock()
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description('')
    get.assert_not_called()


def test_failed_cache_never_becomes_silent_empty_success(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '')
    # First call retries 3x; second call served from the cached error with
    # no further HTTP calls.
    assert get.call_count == 3


def test_search_429_raises_rate_limit_error(monkeypatch):
    resp = Mock(status_code=429)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    assert get.call_count == 3


def test_extract_hidden_input_json_roundtrip():
    html = list_html([raw_job('1', 'Engineer')])
    result = fetcher._extract_hidden_input_json(html, 'jobs')
    assert result[0]['id'] == '1'
    assert result[0]['Posting_Title'] == 'Engineer'


def test_extract_hidden_input_json_missing_marker_returns_none():
    assert fetcher._extract_hidden_input_json('<html></html>', 'jobs') is None
