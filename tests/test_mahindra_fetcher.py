"""Mahindra & Mahindra's J2W classic-theme search, fallback-page detection,
location, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import mahindra_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(fetcher, '_FIRST_PAGE_IDS', {})
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def row(job_id, title, loc='Chennai, Chennai-MRV-AD, IN'):
    return (
        '<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone">'
        f'<a class="jobTitle-link" href="/job/Some-Title/{job_id}/">{title}</a></span></td>'
        '<td class="colLocation hidden-phone"><span class="jobLocation">'
        f'{loc}</span></td></tr>'
    )


def page(rows):
    return '<html><body><table>' + ''.join(rows) + '</table></body></html>'


def fallback_page():
    return (
        '<html><body><label>There are currently no open positions matching "xyz". '
        'The 25 most recent jobs posted by Mahindra &amp; Mahindra Limited are listed below.</label>'
        + row('999999', 'Unrelated Sales Job')
        + '</body></html>'
    )


def mock_response(text):
    return Mock(status_code=200, text=text, raise_for_status=lambda: None)


def test_parses_rows_and_normalises_location(monkeypatch):
    get = Mock(return_value=mock_response(page([row('1418044500', 'Lead Engineer - VES')])))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('software engineer', 'India', num=10, start=0)
    assert jobs == [{
        'id': '1418044500',
        'title': 'Lead Engineer - VES',
        'location': 'Chennai, Chennai-MRV-AD, India',
        'posting_date': '',
        'application_url': 'https://jobs.mahindracareers.com/job/Some-Title/1418044500/',
    }]
    _, kwargs = get.call_args
    assert kwargs['params'] == {'q': 'software engineer', 'locationsearch': 'India'}


def test_fallback_page_treated_as_empty_not_as_real_results(monkeypatch):
    get = Mock(return_value=mock_response(fallback_page()))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('angular', 'India', num=10, start=10)
    assert jobs == []  # NOT the 25 unrelated fallback rows


def test_fallback_page_detected_mid_pagination_for_a_real_keyword(monkeypatch):
    real_page = mock_response(page([row('1', 'Manager A'), row('2', 'Manager B')]))
    overshoot_page = mock_response(fallback_page())
    get = Mock(side_effect=[real_page, overshoot_page])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    first = fetcher.fetch_jobs('manager', 'India', num=10, start=0)
    second = fetcher.fetch_jobs('manager', 'India', num=10, start=500)
    assert len(first) == 2
    assert second == []


def test_non_digit_job_id_is_skipped(monkeypatch):
    bad_row = (
        '<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone">'
        '<a class="jobTitle-link" href="/job/Some-Title/not-a-number/">Bad Job</a></span></td>'
        '<td class="colLocation hidden-phone"><span class="jobLocation">Pune, IN</span></td></tr>'
    )
    get = Mock(return_value=mock_response(page([bad_row])))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    assert fetcher.fetch_jobs('engineer', '', num=10, start=0) == []


@pytest.mark.parametrize('raw,expected', [
    ('Chennai, Chennai-MRV-AD, IN', 'Chennai, Chennai-MRV-AD, India'),
    ('Bengaluru, India', 'Bengaluru, India'),
    ('London, UK', 'London, UK, India'),
    ('', 'India'),
])
def test_normalise_location(raw, expected):
    assert fetcher._normalise_location(raw) == expected


def test_wraparound_guard_stops_replayed_first_page(monkeypatch):
    replay = mock_response(page([row('1', 'Engineer A'), row('2', 'Engineer B')]))
    get = Mock(side_effect=[replay, replay])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    first = fetcher.fetch_jobs('engineer', '', num=10, start=0)
    second = fetcher.fetch_jobs('engineer', '', num=10, start=10)
    assert len(first) == 2
    assert second == []


def test_different_keywords_track_independent_first_pages(monkeypatch):
    same_ids = mock_response(page([row('1', 'Engineer A')]))
    get = Mock(return_value=same_ids)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    fetcher.fetch_jobs('engineer', '', num=10, start=0)
    result = fetcher.fetch_jobs('developer', '', num=10, start=0)
    assert [j['id'] for j in result] == ['1']


def test_search_429_raises_rate_limit_error(monkeypatch):
    resp = Mock(status_code=429)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', '', num=10, start=0)
    assert get.call_count == 3


def test_search_connection_failure_raises_rate_limit_error(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('engineer', '', num=10, start=0)
    assert get.call_count == 3


def test_detail_extracts_description_and_date(monkeypatch):
    response = mock_response('''<span class="jobdescription">
    <p>Build services with C# &amp; .NET.</p></span>
    <meta itemprop="datePosted" content="Thu Aug 13 00:00:00 UTC 2026">''')
    monkeypatch.setattr(fetcher.requests, 'get', Mock(return_value=response))
    assert fetcher.fetch_job_description('https://jobs.mahindracareers.com/job/x/1/') == (
        'Build services with C# & .NET.', '2026-08-13')


def test_detail_request_failure_is_reported(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('unavailable'))
    monkeypatch.setattr(fetcher.requests, 'get', get)
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description('https://jobs.mahindracareers.com/job/x/1/')
    assert get.call_count == 3
