"""Public ATS contracts for all seven lower-priority GCC sources."""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import gcc_public_boards as boards


SLUGS = ['arctera', 'rapid7', 'zendesk', 'litmos', 'kuehnenagel', 'mythic', 'graphcore']


def response(data):
    return SimpleNamespace(json=lambda: data)


@pytest.mark.parametrize('slug', SLUGS)
def test_each_adapter_exports_independent_board(slug):
    m = importlib.import_module(slug + '_fetcher')
    assert callable(m.fetch_jobs) and callable(m.fetch_job_description)
    assert m.RateLimitError is boards.RateLimitError
    assert all(m._board is not importlib.import_module(other + '_fetcher')._board for other in SLUGS if other != slug)


@pytest.mark.parametrize('location,expected', [('Bengaluru', 'Bengaluru, India'), ('Bangalore', 'Bangalore, India'),
                                              ('Pune, India', 'Pune, India'), ('Indiana, USA', ''), ('Remote, US', '')])
def test_location_country_boundaries(location, expected):
    assert boards.india_location(location) == expected


@pytest.mark.parametrize('token', ['litmos', 'graphcore'])
def test_greenhouse_date_content_and_location(token, monkeypatch):
    raw = '&lt;div class="content-intro"&gt;Generative AI employer marketing&lt;/div&gt;&lt;p&gt;Location: Pune. Build C# services.&lt;/p&gt;'
    data = {'meta': {'total': 2}, 'jobs': [
        {'id': 1, 'title': 'Software Engineer', 'location': {'name': 'India'}, 'content': raw,
         'absolute_url': 'https://example.com/jobs/1', 'first_published': '2026-10-01T00:00:00Z', 'updated_at': '2026-10-09'},
        {'id': 2, 'title': 'Software Engineer', 'location': {'name': 'Indiana, USA'}}]}
    calls = []
    monkeypatch.setattr(boards, 'get', lambda *a, **kw: calls.append(a) or response(data))
    b = boards.Greenhouse(token)
    jobs = b.fetch_jobs('ignored', 'India')
    assert len(jobs) == 1 and jobs[0]['posting_date'] == '2026-10-01'
    assert b.fetch_job_description(jobs[0]['application_url'])[0] == 'Location: Pune. Build C# services.'
    if token == 'litmos':
        assert 'Pune' in jobs[0]['location']
    assert b.fetch_jobs('other', 'India', start=1) == []
    assert len(calls) == 1


@pytest.mark.parametrize('data', [{}, {'jobs': [], 'meta': {'total': 1}}])
def test_greenhouse_partial_response_sticky(monkeypatch, data):
    calls = []
    monkeypatch.setattr(boards, 'get', lambda *a: calls.append(a) or response(data))
    b = boards.Greenhouse('graphcore')
    for _ in range(2):
        with pytest.raises(boards.RateLimitError):
            b.fetch_jobs('', 'India')
    assert len(calls) == 1


def test_lever_full_description_original_date_and_city(monkeypatch):
    data = [{'id': 'abc', 'text': 'Software Engineer', 'categories': {'allLocations': ['Bangalore', 'Austin, TX']},
             'description': '<p>Build software.</p>', 'lists': [{'text': 'Skills', 'content': '<li>C#</li>'}],
             'additional': '<p>Apply today</p>', 'hostedUrl': 'https://jobs.lever.co/mythic-ai.com/abc', 'createdAt': 1791504000000}]
    monkeypatch.setattr(boards, 'get', lambda *a: response(data))
    b = boards.Lever('mythic-ai.com'); jobs = b.fetch_jobs('', 'India')
    assert jobs[0]['location'] == 'Bangalore, India'
    assert jobs[0]['posting_date'] == '2026-10-09'
    assert b.fetch_job_description(jobs[0]['application_url'])[0] == 'Build software. Skills C# Apply today'


def test_nested_workday_india_facet():
    assert list(boards.country_facets([{'facetParameter': 'group', 'values': [
        {'facetParameter': 'locationCountry', 'values': [{'descriptor': 'India', 'id': 'in'}]}]}])) == [('locationCountry', 'in')]


def test_workday_country_facet_multilocation_and_exact_date(monkeypatch):
    calls = []
    def post(url, body, timeout):
        calls.append(dict(body))
        if not body['appliedFacets']:
            return {'total': 1, 'jobPostings': [], 'facets': [{'facetParameter': 'locationCountry', 'values': [{'descriptor': 'India', 'id': 'in'}]}]}
        return {'total': 1, 'jobPostings': [{'title': 'Software Engineer', 'externalPath': '/job/Multi/R1', 'locationsText': '2 Locations', 'postedOn': 'Posted 30+ Days Ago'}]}
    monkeypatch.setattr(boards, 'post', post)
    monkeypatch.setattr(boards, 'get', lambda *a: response({'jobPostingInfo': {
        'jobReqId': 'R1', 'title': 'Software Engineer', 'location': 'Pune, India', 'additionalLocations': ['Bengaluru, India'],
        'jobDescription': '<p>C# services</p>', 'startDate': '2026-08-01'}}))
    b = boards.Workday('zendesk.wd1.myworkdayjobs.com', 'zendesk', 'zendesk');j = b.fetch_jobs('', 'India')
    assert calls[1]['appliedFacets'] == {'locationCountry': ['in']}
    assert j[0]['location'] == 'Pune, India; Bengaluru, India'
    assert j[0]['posting_date'] == '2026-08-01'


@pytest.mark.parametrize('content,expected', [
    ('Arctera is seeking an engineer. C# services. About Us: Cloud Software Group.', 1),
    ('Citrix is seeking an engineer. About Us: Portfolio includes Arctera.', 0)])
def test_arctera_identity_not_parent_boilerplate(monkeypatch, content, expected):
    monkeypatch.setattr(boards, 'post', lambda *a: {'total': 1, 'jobPostings': [{'title': 'Software Engineer', 'externalPath': '/job/India/R1', 'locationsText': 'Bengaluru, India'}]})
    monkeypatch.setattr(boards, 'get', lambda *a: response({'jobPostingInfo': {
        'jobReqId': 'R1', 'title': 'Software Engineer', 'location': 'Bengaluru, India', 'jobDescription': content, 'startDate': '2026-10-01'}}))
    b = boards.Workday('tibco.wd5.myworkdayjobs.com', 'tibco', 'Cloud_Software_Group', employer='Arctera')
    assert len(b.fetch_jobs('', 'India')) == expected


@pytest.mark.parametrize('kind', ['workday', 'phenom'])
@pytest.mark.parametrize('repeated', [False, True])
def test_paginated_empty_and_repeat_pages_fail(monkeypatch, kind, repeated):
    b = boards.Workday('x', 'x', 'x') if kind == 'workday' else boards.Phenom()
    page = {'total': 2, 'jobPostings': []} if kind == 'workday' else {'refineSearch': {'status': 200, 'totalHits': 2, 'data': {'jobs': []}}}
    if repeated and kind == 'workday':
        page['jobPostings'] = [{'externalPath': '/job/US/R1', 'locationsText': 'Boston, US'}]
    elif repeated:
        page['refineSearch']['data']['jobs'] = [{'jobId': '1', 'jobSeqNo': 'KUNA1', 'title': 'Engineer',
                                                'country': 'India', 'cityStateCountry': 'Hyderabad, India'}]
    monkeypatch.setattr(boards, 'post', lambda *a: page)
    for _ in range(2):
        with pytest.raises(boards.RateLimitError, match='repeated' if repeated else 'before total'):
            b.fetch_jobs('', 'India')


def test_phenom_country_filter_failure(monkeypatch):
    monkeypatch.setattr(boards, 'post', lambda *a: {'refineSearch': {'status': 200, 'totalHits': 1,
                                                                 'data': {'jobs': [{'jobId': '1', 'country': 'USA'}]}}})
    with pytest.raises(boards.RateLimitError, match='foreign'):
        boards.Phenom().fetch_jobs('', 'India')


def test_post_retries_are_bounded(monkeypatch):
    import requests
    calls = []
    def fail(*a, **kw):
        calls.append(a)
        raise requests.HTTPError('429')
    monkeypatch.setattr(boards.requests, 'post', fail)
    monkeypatch.setattr(boards.time, 'sleep', lambda _: None)
    with pytest.raises(boards.RateLimitError):
        boards.post('https://example.com', {}, 20)
    assert len(calls) == 3


def test_phenom_pagination_details_and_country(monkeypatch):
    calls = []
    def post(url, body, timeout):
        calls.append(dict(body))
        i = body['from']
        return {'refineSearch': {'status': 200, 'totalHits': 2, 'data': {'jobs': [
            {'jobId': str(i), 'jobSeqNo': 'KUNA' + str(i), 'title': 'Software Engineer', 'country': 'India',
             'cityStateCountry': 'Chennai, Tamil Nadu, India', 'postedDate': '2026-10-01'}]}}}
    monkeypatch.setattr(boards, 'post', post)
    job = {'description': '&lt;p&gt;C# engineering&lt;/p&gt;', 'postedDate': '2026-10-01'}
    monkeypatch.setattr(boards, 'get', lambda *a: SimpleNamespace(text='phApp.ddo = ' + json.dumps({'jobDetail': {'data': {'job': job}}}) + ';'))
    b = boards.Phenom();jobs = b.fetch_jobs('', 'India')
    assert len(jobs) == 2 and calls[1]['from'] == 1
    assert calls[0]['selected_fields'] == {'country': ['India']}
    assert 'Tamil Nadu' in jobs[0]['location']
    assert b.fetch_job_description(jobs[0]['application_url']) == ('C# engineering', '2026-10-01')


@pytest.mark.parametrize('slug', SLUGS)
def test_pipeline_failure_never_advances_production_state(slug, monkeypatch, tmp_path):
    import run_company
    job = {'id': 'test-1', 'title': 'Software Engineer', 'location': 'Hyderabad, India', 'description': 'C# engineering',
           'posting_date': '2026-10-09', 'application_url': 'https://example.com', 'tags': ['.NET / C#']}
    monkeypatch.setattr(run_company, 'find_matching_jobs', lambda *a, **kw: (1, [job]))
    state = tmp_path / 'seen.json'
    result = run_company.run_company_pipeline(slug, seen_path=state, notify_func=lambda *a, **kw: False, fetcher=SimpleNamespace())
    assert result['delivery_failed'] and not state.exists()
