from unittest.mock import Mock, patch
from src import blackbaud_fetcher as f

def test_country_facet_and_keyword_specific_wraparound():
    f._FIRST_PAGE_IDS.clear()
    response = Mock()
    response.json.return_value = {'jobPostings': [{'title': 'Software Engineer', 'locationsText': 'Hyderabad - India (Skyview)', 'externalPath': '/job/Hyderabad/Software-Engineer_R001', 'bulletFields': ['R001']}]}
    with patch.object(f, '_post_with_retries', return_value=response) as post:
        assert len(f.fetch_jobs('C#', 'India')) == 1
        assert f.fetch_jobs('C#', 'India', start=20) == []
        assert len(f.fetch_jobs('LangChain', 'India', start=20)) == 1
    body = post.call_args.args[1]
    assert body['appliedFacets'] == {'locationCountry': ['c4f78be1a8f14da0ab49ce1162348a5e']}

def test_description_uses_workday_cxs_not_spa():
    f._desc_cache.clear()
    response = Mock(status_code=200)
    response.json.return_value = {'jobPostingInfo': {'jobDescription': '<p>C# &amp; .NET Core</p>', 'startDate': '2026-10-01'}}
    url = f._JOB_BASE + '/job/Hyderabad/Software-Engineer_R001'
    with patch.object(f.requests, 'get', return_value=response) as get:
        assert f.fetch_job_description(url) == ('C# & .NET Core', '2026-10-01')
    assert '/wday/cxs/blackbaud/ExternalCareers/job/' in get.call_args.args[0]
    assert get.call_args.kwargs['verify'] is True

def test_indianapolis_not_india_and_ambiguous_details_resolve():
    assert not f._is_india('Indianapolis, Indiana')
    assert f._normalize_location('Hyderabad') == 'Hyderabad, India'
    assert f._parse_posted_on('Posted 30+ Days Ago') == ''
    response = Mock(status_code=200)
    response.json.return_value = {'jobPostingInfo': {'country': {'descriptor': 'India'}, 'location': 'Chennai', 'additionalLocations': ['Hyderabad'], 'jobDescription': '<p>C#</p>', 'startDate': '2026-10-01'}}
    with patch.object(f.requests, 'get', return_value=response):
        assert f._resolve_ambiguous_location('/job/Multiple/Engineer_R002', 20) == 'Hyderabad, India'
