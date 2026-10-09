from unittest.mock import Mock, patch
from src import deutscheboerse_fetcher as f

def test_public_key_refresh_and_country_scope():
    f._jobs = None
    f._list_error = None
    f._descriptions.clear()
    page = Mock(text='<script id="__NUXT_DATA__" type="application/json">[{"typesenseApiKey-tenant":1},"public-scoped-key"]</script>')
    response = Mock()
    response.json.return_value = {'results': [{'found': 2, 'hits': [{'document': {'id': 'one', 'external_id': '123-en_GB', 'title': 'Software Engineer', 'country': ['India'], 'location': ['Hyderabad'], 'url': 'https://careers.deutsche-boerse.com/job/one', 'description': '<p>C# .NET</p>'}}, {'document': {'id': 'two', 'title': 'Engineer', 'country': ['United States'], 'location': ['Indianapolis']}}]}]}
    with patch.object(f, '_request', side_effect=[page, response]) as request:
        job = f.fetch_jobs('', 'India')[0]
        assert job['id'] == '123-en_GB'
        assert job['location'] == 'Hyderabad, India'
        assert f.fetch_job_description(job['application_url']) == ('C# .NET', '')
        assert request.call_args.kwargs['params']['x-typesense-api-key'] == 'public-scoped-key'
