"""Air India's tile-theme pagination, location, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import airindia_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher, '_FIRST_PAGE_IDS', None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def page(ids):
    return '<div id="job-tile-list">' + ''.join(
        f'<div class="job-tile-cell"><a class="jobTitle-link" '
        f'href="/job/Engineer/{i}/">Engineer &amp; Developer</a>'
        f'<a class="jobTitle-link" href="/job/Engineer/{i}/">Duplicate</a>'
        f'<div id="job-{i}-tablet-section-location-value">Gurugram, HO</div></div>'
        for i in ids
    ) + '</div>'


def test_paginates_deduplicates_and_reuses_cache(monkeypatch):
    get_page = Mock(side_effect=[page(range(25)), page([24, 25, 26]), page([])])
    monkeypatch.setattr(fetcher, '_fetch_page', get_page)
    jobs = fetcher.fetch_jobs('software', 'India', num=100)
    assert len(jobs) == 27
    assert jobs[0]['title'] == 'Engineer & Developer'
    assert jobs[0]['location'] == 'Gurugram, HO, India'
    assert jobs[0]['application_url'] == 'https://careers.airindia.com/job/Engineer/0/'
    assert [call.args[0] for call in get_page.call_args_list] == [0, 25, 50]
    assert fetcher.fetch_jobs('different', '', start=20, num=20) == jobs[20:]
    assert get_page.call_count == 3


def test_stops_on_replayed_page(monkeypatch):
    get_page = Mock(return_value=page([1, 2]))
    monkeypatch.setattr(fetcher, '_fetch_page', get_page)
    assert len(fetcher.fetch_jobs('', '')) == 2
    assert get_page.call_count == 2


@pytest.mark.parametrize('raw,expected', [
    (' Mumbai, Western ', 'Mumbai, Western, India'),
    ('Bengaluru, India', 'Bengaluru, India'),
    ('London, UK', 'London, UK'),
    ('HO', 'HO'),
    ('', ''),
])
def test_normalizes_only_evidenced_india_locations(raw, expected):
    assert fetcher._normalise_location(raw) == expected


@pytest.mark.parametrize('result', ['<html>Access denied</html>', fetcher.RateLimitError('down')])
def test_failed_cache_never_becomes_silent_empty_success(monkeypatch, result):
    get_page = Mock(**({'side_effect': result} if isinstance(result, Exception) else {'return_value': result}))
    monkeypatch.setattr(fetcher, '_fetch_page', get_page)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '')
    assert get_page.call_count == 1


def test_detail_extracts_description_and_date(monkeypatch):
    response = Mock(status_code=200, text='''<span class="jobdescription">
    <p>Build services with C# &amp; .NET.</p></span>
    <meta itemprop="datePosted" content="Tue Aug 25 00:00:00 UTC 2026">''')
    monkeypatch.setattr(fetcher.requests, 'get', Mock(return_value=response))
    assert fetcher.fetch_job_description('https://careers.airindia.com/job/1/') == (
        'Build services with C# & .NET.', '2026-08-25')


def test_detail_request_failure_is_reported(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('unavailable'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description('https://careers.airindia.com/job/1/')
    assert get.call_count == 3
