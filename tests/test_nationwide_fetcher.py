from unittest.mock import Mock, patch
from src import nationwide_fetcher as f

def test_nonstandard_bulletfields_give_location_and_requisition():
    response = Mock()
    response.json.return_value = {'jobPostings': [{'title': 'Software Engineer', 'externalPath': '/job/Hyderabad/Engineer_099967-1', 'bulletFields': ['Telangana - Hyderabad, Mindspace', '099967', 'Software Engineer']}]}
    with patch.object(f, '_post_with_retries', return_value=response):
        job = f.fetch_jobs('', 'India')[0]
    assert job['id'] == '099967'
    assert job['location'] == 'Telangana - Hyderabad, Mindspace, India'
    assert '/Nationwide_Career_India/job/' in job['application_url']
