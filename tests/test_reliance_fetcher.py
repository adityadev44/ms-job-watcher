"""Reliance Industries' GridView pagination, location, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import reliance_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher, '_FIRST_PAGE_IDS', None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def row(job_id, title, func='Manufacturing', loc='Jamnagar', date='03 Sep 2026'):
    return (
        f'<a id="MainContent_rgJobs_hylUser_0" href="frmJobSearch.aspx?JBTITLE=x&amp;jbID=y" '
        f'href="ignored">{title} ( {job_id} )</a></td>'
        f'<td>{func}</td><td>{loc}</td><td>{date}</td>'
    )


def next_btn(control='ctl13', disabled=False):
    dis = ' disabled="disabled"' if disabled else ''
    return (
        f'<input type="submit" name="ctl00$MainContent$rgJobs${control}$lnkNext"'
        f' id="MainContent_rgJobs_lnkNext"{dis}/>'
    )


def page(rows, control='ctl13', disabled=False):
    return (
        '<html><body><form><table id="MainContent_rgJobs">'
        + ''.join(rows)
        + next_btn(control, disabled)
        + '</table></form></body></html>'
    )


def test_paginates_deduplicates_and_reuses_cache(monkeypatch):
    page1 = page([row(i, 'Engineer') for i in range(10)], control='ctl13', disabled=False)
    page2 = page([row(i, 'Engineer') for i in range(10, 13)], control='ctl09', disabled=True)

    get_mock = Mock(return_value=Mock(status_code=200, text=page1))
    post_mock = Mock(return_value=Mock(status_code=200, text=page2))
    session = Mock()
    session.request = Mock(side_effect=[
        Mock(status_code=200, text=page1, raise_for_status=lambda: None),
        Mock(status_code=200, text=page2, raise_for_status=lambda: None),
    ])
    monkeypatch.setattr(fetcher.requests, 'Session', lambda: session)

    jobs = fetcher.fetch_jobs('software', 'India', num=100)
    assert len(jobs) == 13
    assert jobs[0]['id'] == '0'
    assert jobs[0]['title'] == 'Engineer ( 0 )'
    assert jobs[0]['location'] == 'Jamnagar, India'
    assert jobs[0]['posting_date'] == '2026-09-03'
    assert jobs[0]['application_url'] == (
        'https://careers.ril.com/rilcareers/frmJobSearch.aspx?JBTITLE=x&jbID=y'
    )
    assert session.request.call_count == 2

    # Second call reuses the cache -- no further HTTP calls.
    assert fetcher.fetch_jobs('different', '', start=10, num=20) == jobs[10:]
    assert session.request.call_count == 2


def test_stops_on_disabled_next_button_first_page(monkeypatch):
    single_page = page([row(1, 'Engineer'), row(2, 'Engineer')], disabled=True)
    session = Mock()
    session.request = Mock(return_value=Mock(status_code=200, text=single_page, raise_for_status=lambda: None))
    monkeypatch.setattr(fetcher.requests, 'Session', lambda: session)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 2
    assert session.request.call_count == 1


def test_stops_on_replayed_page(monkeypatch):
    looping_page = page([row(1, 'Engineer'), row(2, 'Engineer')], disabled=False)
    session = Mock()
    session.request = Mock(return_value=Mock(status_code=200, text=looping_page, raise_for_status=lambda: None))
    monkeypatch.setattr(fetcher.requests, 'Session', lambda: session)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 2
    # First GET, then one POST that replays the same page 1 ids -> stop.
    assert session.request.call_count == 2


@pytest.mark.parametrize('raw,expected', [
    ('Jamnagar', 'Jamnagar, India'),
    (' Navi Mumbai ', 'Navi Mumbai, India'),
    ('Bengaluru, India', 'Bengaluru, India'),
    ('London', 'London'),
    ('', ''),
])
def test_normalizes_only_evidenced_india_locations(raw, expected):
    assert fetcher._normalise_location(raw) == expected


@pytest.mark.parametrize('raw,expected', [
    ('03 Sep 2026', '2026-09-03'),
    ('11 May 2026', '2026-05-11'),
    ('', ''),
    ('garbage', ''),
])
def test_parse_date(raw, expected):
    assert fetcher._parse_date(raw) == expected


def test_failed_cache_never_becomes_silent_empty_success_missing_container(monkeypatch):
    session = Mock()
    session.request = Mock(return_value=Mock(
        status_code=200, text='<html>Access denied</html>', raise_for_status=lambda: None
    ))
    monkeypatch.setattr(fetcher.requests, 'Session', lambda: session)

    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '')
    # Only the first fetch_jobs call issues the (single, non-retried) GET that
    # discovers the missing results table; the second is served from the
    # cached error with no further HTTP calls.
    assert session.request.call_count == 1


def test_failed_cache_never_becomes_silent_empty_success_connection_error(monkeypatch):
    session = Mock()
    session.request = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'Session', lambda: session)

    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '')
    # First fetch_jobs call retries 3x inside _request before giving up; the
    # second call is served straight from the cached error, no further calls.
    assert session.request.call_count == 3


def test_detail_extracts_description_and_date(monkeypatch):
    response = Mock(status_code=200, text='''
    <span id="MainContent_lblSummRole"><p>.</p><p>Build services with C# &amp; .NET.</p></span>
    <span id="MainContent_lblEduReq">Bachelors degree</span>
    <span id="MainContent_lblPostedDate">03 Sep 2026</span>
    ''')
    response.raise_for_status = lambda: None
    monkeypatch.setattr(fetcher.requests, 'get', Mock(return_value=response))
    description, date = fetcher.fetch_job_description('https://careers.ril.com/rilcareers/frmJobSearch.aspx?JBTITLE=x&jbID=y')
    assert description == 'Build services with C# & .NET. Bachelors degree'
    assert date == '2026-09-03'


def test_detail_request_failure_is_reported(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('unavailable'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description('https://careers.ril.com/rilcareers/frmJobSearch.aspx?JBTITLE=x&jbID=y')
    assert get.call_count == 3
