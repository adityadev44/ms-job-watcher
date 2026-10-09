from unittest.mock import Mock, patch
from src import lplfinancial_fetcher as f

def test_india_board_bare_hyderabad_is_normalized():
    response = Mock()
    response.json.return_value = {'jobPostings': [{'title': 'Software Engineer', 'externalPath': '/job/Hyderabad/Engineer_R-049420', 'locationsText': 'Hyderabad', 'bulletFields': ['R-049420']}]}
    with patch.object(f, '_post_with_retries', return_value=response) as post:
        job = f.fetch_jobs('', 'India')[0]
    assert job['id'] == 'R-049420'
    assert job['location'] == 'Hyderabad, India'
    assert '/India_Ext/job/' in job['application_url']
    assert post.call_args.args[1]['appliedFacets'] == {}
