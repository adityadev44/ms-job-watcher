from unittest.mock import Mock, patch
from src import amtechsoftware_fetcher as f

def test_bangalore_normalization_and_publication_date():
    f._cache_filled = False
    f._cache_error = None
    f._content_cache.clear()
    response = Mock(status_code=200)
    response.json.return_value = {'jobs': [
        {'id': 1, 'title': 'Senior AI Engineer', 'location': {'name': 'Bangalore'}, 'content': '&lt;p&gt;LangChain&lt;/p&gt;', 'updated_at': '2026-10-01'},
        {'id': 2, 'title': 'Engineer', 'location': {'name': 'United States'}},
    ]}
    with patch.object(f.requests, 'get', return_value=response):
        jobs = f.fetch_jobs('', 'India')
        assert len(jobs) == 1 and jobs[0]['location'] == 'Bangalore, India'
        assert f.fetch_job_description(jobs[0]['application_url']) == ('LangChain', '')

def test_portfolio_ai_marketing_is_not_role_skill():
    description = f._strip_html('<p>About Vista Equity Partners</p><p>Generative AI across the portfolio</p><h2>Role Description</h2><p>Java and AWS</p>')
    assert description == 'Role Description Java and AWS'
    assert 'Generative AI' not in description
