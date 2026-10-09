"""Verified GitHub and software-product Greenhouse board contracts."""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import gcc_public_boards as public
import developer_cloud_boards as developer

SLUGS = ['github', 'jetbrains', 'datadog', 'elastic', 'newrelic']


def response(data):
    return SimpleNamespace(json=lambda: data)


@pytest.mark.parametrize('slug', SLUGS)
def test_exported_contract_and_separate_cache(slug):
    module = importlib.import_module(slug + '_fetcher')
    assert callable(module.fetch_jobs) and callable(module.fetch_job_description)
    assert module.RateLimitError is public.RateLimitError
    for other in SLUGS:
        if other != slug:
            assert module._board is not importlib.import_module(other + '_fetcher')._board


@pytest.mark.parametrize('token', ['jetbrains', 'datadog', 'elastic', 'newrelic'])
def test_greenhouse_original_date_marketing_and_country(token, monkeypatch):
    monkeypatch.setattr(public, 'get', lambda *a: response({'meta': {'total': 2}, 'jobs': [
        {'id': 1, 'title': 'Software Engineer', 'location': {'name': 'Bangalore, India'},
         'content': '&lt;div class="content-intro"&gt;Generative AI employer marketing&lt;/div&gt;'
                    '&lt;p&gt;Build Java services.&lt;/p&gt;',
         'absolute_url': 'https://example.com/job/1', 'first_published': '2026-09-01', 'updated_at': '2026-10-09'},
        {'id': 2, 'title': 'Software Engineer', 'location': {'name': 'Indiana, USA'}}]}))
    board = public.Greenhouse(token)
    jobs = board.fetch_jobs('ignored', 'India')
    assert len(jobs) == 1
    assert jobs[0]['posting_date'] == '2026-09-01'
    assert board.fetch_job_description(jobs[0]['application_url'])[0] == 'Build Java services.'


def github_row(jid, country='India', location='India Remote'):
    return {'data': {'req_id': jid, 'client_code': 'githubinc', 'title': 'Software Engineer',
                     'country': country, 'location_name': location, 'posted_date': '2026-09-01T00:00:00Z',
                     'description': '<p>Build systems.</p>', 'qualifications': '<p>C# required.</p>',
                     'responsibilities': '<p>Ship services.</p>'}}


def test_github_full_pagination_fields_cache_and_remote_country(monkeypatch):
    calls = []
    def get(url, timeout, **kwargs):
        calls.append(kwargs['params']['page'])
        rows = [github_row('1'), github_row('2', 'United States', 'Indiana Remote')] if len(calls) == 1 else [github_row('3', location='Pune, India')]
        return response({'jobs': rows, 'totalCount': 3})
    monkeypatch.setattr(developer, 'get', get)
    board = developer.GitHub()
    jobs = board.fetch_jobs('', 'India', num=100)
    assert [j['id'] for j in jobs] == ['1', '3']
    assert 'Pune' in jobs[1]['location']
    assert jobs[0]['posting_date'] == '2026-09-01'
    assert jobs[0]['application_url'] == 'https://www.github.careers/jobs/1?lang=en-us'
    assert board.fetch_job_description(jobs[0]['application_url'])[0] == 'Build systems. C# required. Ship services.'
    assert board.fetch_jobs('other', 'India', num=1, start=1) == [jobs[1]]
    assert calls == [1, 2]


@pytest.mark.parametrize('data', [{}, {'jobs': [], 'totalCount': 1},
                                  {'jobs': [github_row('1'), github_row('1')], 'totalCount': 2}])
def test_github_malformed_and_incomplete_are_sticky_errors(data, monkeypatch):
    calls = []
    monkeypatch.setattr(developer, 'get', lambda *a, **kw: calls.append(a) or response(data))
    board = developer.GitHub()
    for _ in range(2):
        with pytest.raises(public.RateLimitError):
            board.fetch_jobs('', 'India')
    assert len(calls) == 1


def test_github_genuine_zero_india_valid(monkeypatch):
    monkeypatch.setattr(developer, 'get', lambda *a, **kw: response({'jobs': [github_row('1', 'United Kingdom', 'UK Remote')], 'totalCount': 1}))
    assert developer.GitHub().fetch_jobs('', 'India') == []


def test_github_foreign_employer_rejected(monkeypatch):
    row = github_row('1')
    row['data']['client_code'] = 'unrelated'
    monkeypatch.setattr(developer, 'get', lambda *a, **kw: response({'jobs': [row], 'totalCount': 1}))
    with pytest.raises(public.RateLimitError):
        developer.GitHub().fetch_jobs('', 'India')


def test_github_repeated_page_rejected(monkeypatch):
    monkeypatch.setattr(developer, 'get', lambda *a, **kw: response({'jobs': [github_row('1')], 'totalCount': 2}))
    with pytest.raises(public.RateLimitError):
        developer.GitHub().fetch_jobs('', 'India')


def test_github_empty_description_rejected(monkeypatch):
    row = github_row('1')
    for field in ['description', 'qualifications', 'responsibilities']:
        row['data'][field] = ''
    monkeypatch.setattr(developer, 'get', lambda *a, **kw: response({'jobs': [row], 'totalCount': 1}))
    with pytest.raises(public.RateLimitError):
        developer.GitHub().fetch_jobs('', 'India')


def test_github_total_changes_rejected(monkeypatch):
    pages = iter([{'jobs': [github_row('1')], 'totalCount': 2},
                  {'jobs': [github_row('2')], 'totalCount': 3}])
    monkeypatch.setattr(developer, 'get', lambda *a, **kw: response(next(pages)))
    with pytest.raises(public.RateLimitError):
        developer.GitHub().fetch_jobs('', 'India')
