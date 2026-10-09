import importlib
import json
import html
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import collaboration_saas_boards as boards
from gcc_public_boards import RateLimitError


@pytest.mark.parametrize('slug', ['asana', 'airtable', 'figma', 'box', 'dropbox',
                                  'notion', 'clickup', 'mondaycom', 'miro'])
def test_contract(slug):
    module = importlib.import_module(slug + '_fetcher')
    assert callable(module.fetch_jobs)
    assert callable(module.fetch_job_description)
    assert module.RateLimitError is RateLimitError


def test_boards_do_not_share_caches():
    slugs = ['asana', 'airtable', 'figma', 'box', 'dropbox', 'notion', 'clickup', 'mondaycom', 'miro']
    instances = [importlib.import_module(slug + '_fetcher')._board for slug in slugs]
    assert len({id(board) for board in instances}) == len(slugs)
    assert len({id(board.descriptions) for board in instances}) == len(slugs)


def ashby_job(**kwargs):
    job = {'id': 'abc', 'title': 'Software Engineer', 'location': 'US Remote',
           'jobUrl': 'https://jobs.ashbyhq.com/test/abc', 'publishedAt': '2026-10-01T12:00:00Z',
           'descriptionHtml': '<h1>Who We Are</h1><p>Generative AI company.</p>'
                              '<h2>About the Role</h2><p>Build C# and .NET services.</p>',
           'secondaryLocations': [], 'isListed': True}
    job.update(kwargs)
    return job


def mock_json(monkeypatch, payload):
    class Response:
        def json(self):
            return payload
    monkeypatch.setattr(boards, 'get', lambda *a: Response())


def test_secondary_location_date_and_marketing(monkeypatch):
    mock_json(monkeypatch, {'jobs': [ashby_job(secondaryLocations=[
        {'location': 'India', 'address': {'postalAddress': {
            'addressCountry': 'India', 'addressLocality': 'Pune'}}}])]})
    board = boards.Ashby('test')
    jobs = board.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert 'Pune' in jobs[0]['location']
    assert jobs[0]['posting_date'] == '2026-10-01'
    description, date = board.fetch_job_description(jobs[0]['application_url'])
    assert 'Generative AI company' not in description
    assert '.NET services' in description


@pytest.mark.parametrize('location', ['Remote', 'US Remote', 'Indiana, US', 'APAC'])
def test_remote_not_implicitly_india(monkeypatch, location):
    mock_json(monkeypatch, {'jobs': [ashby_job(location=location)]})
    assert boards.Ashby('test').fetch_jobs('', 'India') == []


@pytest.mark.parametrize('payload', [{}, {'jobs': None}, {'jobs': [ashby_job(), ashby_job()]}])
def test_invalid_inventory_sticky(monkeypatch, payload):
    mock_json(monkeypatch, payload)
    board = boards.Ashby('test')
    with pytest.raises(RateLimitError):
        board.fetch_jobs('', 'India')
    monkeypatch.setattr(boards, 'get', lambda *a: pytest.fail('must not retry failed board'))
    with pytest.raises(RateLimitError):
        board.fetch_jobs('C#', 'India')


def test_miro_real_zero_country_locations(monkeypatch):
    monkeypatch.setattr(boards, 'next_data', lambda *a: {'jobs': [
        {'id': 1, 'title': 'Engineer', 'location': 'New York, US', 'offices': []}]})
    assert boards.Miro().fetch_jobs('', 'India') == []


def test_miro_india_office_detail_and_no_invented_date(monkeypatch):
    def data(url, timeout):
        if 'open-positions' in url:
            return {'jobs': [{'id': 1, 'title': 'Engineer', 'location': 'India',
                             'offices': [{'location': 'Bengaluru, India'}]}]}
        return {'slug': '1', 'content': '<p>Build ASP.NET services.</p>'}
    monkeypatch.setattr(boards, 'next_data', data)
    jobs = boards.Miro().fetch_jobs('', 'India')
    assert 'Bengaluru' in jobs[0]['location']
    assert jobs[0]['posting_date'] == ''


def test_cache_once_and_pagination(monkeypatch):
    mock_json(monkeypatch, {'jobs': [ashby_job(location='Hyderabad, India')]})
    board = boards.Ashby('test')
    assert len(board.fetch_jobs('', 'India', num=1)) == 1
    monkeypatch.setattr(boards, 'get', lambda *a: pytest.fail('must use cache'))
    assert board.fetch_jobs('different keyword', 'India', start=1) == []


@pytest.mark.parametrize('escape', [False, True])
def test_nested_role_headings_preserved(escape):
    raw = '<div><h1>Who We Are</h1><p>Generative AI company.</p></div>' \
          '<div><h2>About the Role</h2><p>Build C# services.</p>' \
          '<section><h3>Requirements</h3><p>Use ASP.NET.</p></section></div>'
    if escape:
        raw = html.escape(html.escape(raw))
    result = boards.role_text(raw)
    assert 'Generative AI company' not in result
    assert 'Build C# services' in result
    assert 'Use ASP.NET' in result


def test_structured_foreign_country_overrides_city(monkeypatch):
    mock_json(monkeypatch, {'jobs': [ashby_job(location='Hyderabad', address={
        'postalAddress': {'addressCountry': 'Pakistan'}})]})
    assert boards.Ashby('test').fetch_jobs('', 'India') == []
