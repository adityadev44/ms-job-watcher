from unittest.mock import Mock, patch
from src import netsmart_fetcher as f

def test_official_india_location_and_keyword_wraparound():
    f._FIRST_PAGE_IDS.clear()
    response = Mock()
    response.json.return_value = {'jobPostings': [{'title': 'Software Engineer .NET', 'locationsText': 'Bengaluru, India', 'externalPath': '/job/Bengaluru/Engineer_R015725', 'bulletFields': ['R015725']}]}
    with patch.object(f, '_post_with_retries', return_value=response) as post:
        assert f.fetch_jobs('.NET', 'India')[0]['id'] == 'R015725'
        assert f.fetch_jobs('.NET', 'India', start=20) == []
        assert f.fetch_jobs('Python', 'India', start=20)
    assert post.call_args.args[1]['appliedFacets'] == {'locations': ['0fa7477b24ac1001b062160629070000']}
