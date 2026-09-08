"""Sapiens' classic SAP SuccessFactors J2W HTML scraping and pagination contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import sapiens_fetcher as fetcher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def _row(job_id, title, location='Bangalore, IN'):
    return f'''
    <tr class="data-row">
        <td class="colTitle">
            <span class="jobTitle hidden-phone">
                <a href="/job/Bangalore-{title.replace(' ', '-')}/{job_id}/" class="jobTitle-link">{title}</a>
            </span>
        </td>
        <td class="colLocation hidden-phone">
            <span class="jobLocation">
                {location}
            </span>
        </td>
    </tr>
    '''


def search_page(rows_html, status_code=200):
    body = '<html><body><table><tbody>' + ''.join(rows_html) + '</tbody></table></body></html>'
    return Mock(status_code=status_code, raise_for_status=lambda: None, text=body)


def test_fetch_jobs_parses_rows(monkeypatch):
    get = Mock(return_value=search_page([_row('1431892333', 'Senior AI Engineer')]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('engineer', 'India', num=20, start=0)
    assert len(jobs) == 1
    job = jobs[0]
    assert job['id'] == '1431892333'
    assert job['title'] == 'Senior AI Engineer'
    assert job['location'] == 'Bangalore, India'
    assert job['application_url'] == (
        'https://careers.sapiens.com/job/Bangalore-Senior-AI-Engineer/1431892333/'
    )

    _, kwargs = get.call_args
    # title is intentionally always blank -- server-side title search is a
    # literal-substring match, not OR-token, so the keyword arg is ignored
    # (see _IGNORES_KEYWORDS rationale in sapiens_fetcher.py).
    assert kwargs['params']['title'] == ''
    assert kwargs['params']['locationsearch'] == 'India'


def test_country_code_normalized_to_india(monkeypatch):
    # Site renders "City, IN" (ISO country code), not "City, India" --
    # matcher.py's Layer 1 check is a literal "india" substring test, so
    # this normalization is load-bearing.
    get = Mock(return_value=search_page([_row('1', 'AI Engineer', location='Bangalore, IN')]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert jobs[0]['location'] == 'Bangalore, India'
    assert 'india' in jobs[0]['location'].lower()


def test_html_entities_unescaped_in_title_and_location(monkeypatch):
    get = Mock(return_value=search_page([
        _row('1401638733', 'Lead Developer - R&amp;D', location='Bangalore, IN')
    ]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert jobs[0]['title'] == 'Lead Developer - R&D'


def test_pagination_assembles_contiguous_rows_across_site_page_size(monkeypatch):
    # Site's own page size is 15; fetch_jobs must assemble 20 without gaps.
    page1_rows = [_row(str(i), f'Engineer {i}') for i in range(15)]
    page2_rows = [_row(str(i), f'Engineer {i}') for i in range(15, 20)]
    page3_rows = [_row(str(i), f'Engineer {i}') for i in range(20, 22)]

    get = Mock(side_effect=[
        search_page(page1_rows),
        search_page(page2_rows),
    ])
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert len(jobs) == 20
    assert [j['id'] for j in jobs] == [str(i) for i in range(20)]

    startrows = [c.kwargs['params']['startrow'] for c in get.call_args_list]
    assert startrows == [0, 15]

    # Next matcher call picks up exactly where the previous one left off.
    get2 = Mock(return_value=search_page(page3_rows))
    monkeypatch.setattr(fetcher.requests, 'get', get2)
    jobs2 = fetcher.fetch_jobs('', 'India', num=20, start=20)
    assert [j['id'] for j in jobs2] == ['20', '21']


def test_missing_title_or_id_dropped(monkeypatch):
    bad_row = '''
    <tr class="data-row">
        <td class="colTitle">
            <span class="jobTitle hidden-phone">
                <a href="/job/x/notanumber/" class="jobTitle-link">Some Role</a>
            </span>
        </td>
        <td class="colLocation hidden-phone">
            <span class="jobLocation">Bangalore, IN</span>
        </td>
    </tr>
    '''
    get = Mock(return_value=search_page([bad_row]))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India', num=20, start=0)
    assert jobs == []


def test_fetch_jobs_retries_then_raises_rate_limit_error(monkeypatch):
    get = Mock(side_effect=requests.ConnectionError('boom'))
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert get.call_count == 3


def test_fetch_jobs_429_retries_then_raises_rate_limit_error(monkeypatch):
    resp = Mock(status_code=429)
    get = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', 'India')
    assert get.call_count == 3


def detail_page(description='<p>Build <b>AI</b> features.</p>',
                 date_str='Mon Aug 31 00:00:00 UTC 2026', status_code=200):
    body = (
        '<html><body>'
        f'<meta itemprop="datePosted" content="{date_str}">'
        '<div><span class="jobdescription">' + description + '</span></div>'
        '</body></html>'
    )
    return Mock(status_code=status_code, raise_for_status=lambda: None, text=body)


def test_fetch_job_description_strips_html_and_parses_date(monkeypatch):
    get = Mock(return_value=detail_page())
    monkeypatch.setattr(fetcher.requests, 'get', get)

    description, posting_date = fetcher.fetch_job_description(
        'https://careers.sapiens.com/job/Bangalore-Senior-AI-Engineer/1431892333/'
    )
    assert description == 'Build AI features.'
    assert posting_date == '2026-08-31'


def test_fetch_job_description_empty_url_returns_empty(monkeypatch):
    assert fetcher.fetch_job_description('') == ('', '')
