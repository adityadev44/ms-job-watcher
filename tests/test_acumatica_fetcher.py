from unittest.mock import Mock, patch
from src import acumatica_fetcher as f

def test_search_uses_verified_country_and_page_offset():
    response = Mock(status_code=200)
    response.json.return_value = {'content': [
        {'id': '123', 'name': 'Software Developer', 'location': {'country': 'in', 'city': 'Hyderabad'}, 'releasedDate': '2026-10-01T12:00:00Z'},
        {'id': '456', 'name': 'Software Developer', 'location': {'country': 'us', 'city': 'Seattle'}},
    ]}
    with patch.object(f.requests, 'get', return_value=response) as get:
        jobs = f.fetch_jobs('.NET', 'India', start=100, num=200)
    assert [j['id'] for j in jobs] == ['123']
    assert jobs[0]['location'] == 'Hyderabad, India'
    assert get.call_args.kwargs['params'] == {'q': '.NET', 'country': 'in', 'limit': 100, 'offset': 100}

def test_description_excludes_company_boilerplate():
    f._desc_cache.clear()
    response = Mock(status_code=200)
    response.json.return_value = {'releasedDate': '2026-10-01', 'jobAd': {'sections': {'companyDescription': {'text': 'generative ai irrelevant'}, 'jobDescription': {'text': '<p>C# .NET</p>'}, 'qualifications': {'text': '3 years'}}}}
    with patch.object(f.requests, 'get', return_value=response):
        description, date = f.fetch_job_description(f._PUBLIC_BASE + '/123-role')
    assert description == 'C# .NET 3 years'
    assert date == '2026-10-01'

def test_country_required_and_tamil_nadu_preserved():
    response = Mock(status_code=200)
    response.json.return_value = {'content': [
        {'id': 'missing', 'name': 'Engineer', 'location': {'city': 'Hyderabad'}},
        {'id': 'tn', 'name': 'Engineer', 'location': {'country': 'in', 'city': 'Coimbatore', 'region': 'TN'}},
    ]}
    with patch.object(f.requests, 'get', return_value=response):
        jobs = f.fetch_jobs('', 'India')
    assert [j['id'] for j in jobs] == ['tn']
    assert jobs[0]['location'] == 'Coimbatore, Tamil Nadu, India'

def test_exhausted_description_requests_raise():
    import pytest
    import requests
    f._desc_cache.clear()
    with patch.object(f.requests, 'get', side_effect=requests.ConnectionError('offline')), patch.object(f.time, 'sleep'), pytest.raises(f.RateLimitError):
        f.fetch_job_description(f._PUBLIC_BASE + '/unavailable')
