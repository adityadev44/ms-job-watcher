"""Akasa Air's Zoho Recruit hidden-input extraction, location dedup, and
failure contracts (mocked at the HTTP seam — no real network calls)."""
import html as html_mod
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import akasaair_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_desc_cache', {})
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def _job(job_id, title, city=None, country='India', desc='<p>Build stuff.</p>', date='2023-06-23'):
    j = {'id': job_id, 'Posting_Title': title, 'Country': country,
         'Job_Description': desc, 'Date_Opened': date}
    if city is not None:
        j['City'] = city
    return j


def _page_html(jobs, decoy_inputs=True):
    """Build a page matching the real tenant's HTML shape: an unrelated
    earlier hidden `value="[{...` input (e.g. moduleMeta) BEFORE the real
    `id="jobs"` one, to exercise the suffix-anchored extraction."""
    blob = html_mod.escape(json.dumps(jobs), quote=True)
    decoy = '<input type="hidden" value="[{&#34;module_name&#34;:&#34;Leads&#34;}]" id="moduleMeta">' if decoy_inputs else ''
    return f'<html><body>{decoy}<input type="hidden" value="{blob}" id="jobs"></body></html>'


def test_fills_cache_past_decoy_input_and_slices(monkeypatch):
    jobs = [
        _job('1', 'Senior Development Engineer - Python', city='Mumbai'),
        _job('2', 'Cabin Crew', city='Pan-India'),
        _job('3', 'Jr.AME', city='NA'),
    ]
    get = Mock(return_value=Mock(status_code=200, text=_page_html(jobs)))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    result = fetcher.fetch_jobs('python developer', 'India', num=2, start=0)
    assert len(result) == 2
    assert result[0]['id'] == '1'
    assert result[0]['location'] == 'Mumbai, India'
    assert result[0]['application_url'] == 'https://akasaair.zohorecruit.in/jobs/Careers/1'

    # keyword ignored server-side; second call reuses cache, no re-fetch
    all_jobs = fetcher.fetch_jobs('totally different', '', num=100, start=0)
    assert len(all_jobs) == 3
    assert get.call_count == 1


@pytest.mark.parametrize('city,country,expected', [
    ('Mumbai', 'India', 'Mumbai, India'),
    ('Pan-India', 'India', 'Pan-India'),   # "india" substring already in city
    ('PAN India', 'India', 'PAN India'),
    ('NA', 'India', 'NA, India'),
    (None, 'India', 'India'),
])
def test_location_dedup(monkeypatch, city, country, expected):
    jobs = [_job('1', 'X', city=city, country=country)]
    get = Mock(return_value=Mock(status_code=200, text=_page_html(jobs)))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    result = fetcher.fetch_jobs('', '', num=100)
    assert result[0]['location'] == expected


def test_description_and_date_served_from_cache(monkeypatch):
    jobs = [_job('42', 'Senior Development Engineer - Python', city='Mumbai',
                  desc='<p>Build <b>Python</b> systems.</p>', date='2023-06-23')]
    get = Mock(return_value=Mock(status_code=200, text=_page_html(jobs)))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    fetcher.fetch_jobs('', '', num=100)
    desc, date = fetcher.fetch_job_description('https://akasaair.zohorecruit.in/jobs/Careers/42')
    assert desc == 'Build Python systems.'
    assert date == '2023-06-23'


def test_missing_jobs_input_never_becomes_silent_empty_success(monkeypatch):
    get = Mock(return_value=Mock(status_code=200, text='<html><body>no jobs here</body></html>'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '', num=100)
    assert get.call_count == 1


def test_429_raises_ratelimiterror(monkeypatch):
    get = Mock(return_value=Mock(status_code=429))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '', num=100)


def test_connection_failure_raises_ratelimiterror_after_retries(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '', num=100)
    assert get.call_count == 3
