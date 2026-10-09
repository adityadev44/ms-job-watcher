from unittest.mock import Mock, patch
from src import starrez_fetcher as f

def test_board_cache_filters_country_and_decodes_content():
    f._cache_filled = False
    f._cache_error = None
    f._india_cache = []
    f._content_cache.clear()
    response = Mock()
    response.status_code = 200
    response.json.return_value = {'jobs': [
        {'id': 1, 'title': 'Software Engineer', 'location': {'name': 'Hyderabad, India'}, 'first_published': '2026-10-01T12:00:00Z', 'updated_at': '2026-10-08', 'content': '&amp;lt;p&amp;gt;C# .NET&amp;lt;/p&amp;gt;'},
        {'id': 2, 'title': 'Software Engineer', 'location': {'name': 'Indianapolis, USA'}},
    ]}
    with patch.object(f.requests, 'get', return_value=response) as get:
        jobs = f.fetch_jobs('ignored', 'India')
        assert [j['id'] for j in jobs] == ['1']
        assert f.fetch_job_description(jobs[0]['application_url']) == ('C# .NET', '2026-10-01')
        assert f.fetch_jobs('different', 'India', start=1) == []
        assert get.call_count == 1

def test_rate_limit_is_visible():
    response = Mock(status_code=429)
    import pytest
    with patch.object(f.requests, 'get', return_value=response), patch.object(f.time, 'sleep'), pytest.raises(f.RateLimitError):
        f._get_with_retry('https://example.test', 1, 'test')

def test_missing_publication_date_and_latched_failure():
    assert f._parse_date({'updated_at': '2026-10-08'}) == ''
    f._cache_filled = False
    f._cache_error = None
    import pytest
    with patch.object(f, '_get_with_retry', side_effect=f.RateLimitError('offline')) as get:
        with pytest.raises(f.RateLimitError):
            f.fetch_jobs('', 'India')
        with pytest.raises(f.RateLimitError):
            f.fetch_jobs('another', 'India')
        assert get.call_count == 1
    f._cache_filled = False
    f._cache_error = None
