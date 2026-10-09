from unittest.mock import patch
from bs4 import BeautifulSoup
from src import itineris_fetcher as f

def test_uuid_list_and_description_scope():
    f._jobs = None
    f._descriptions.clear()
    url = f._LIST_URL + 'detail/ai-engineer/65573aaf-b767-49b1-85b1-980807ffaa75/'
    listing = BeautifulSoup(f'<a href="{url}">AI Engineer</a><a href="{url}">AI Engineer</a><a href="/job-profiles/">Profiles</a>', 'html.parser')
    detail = BeautifulSoup('<nav>generic C# marketing</nav><div class="hibob-job-details-page-container"><h1>AI Engineer</h1><p>LangChain production engineering</p></div>', 'html.parser')
    with patch.object(f, '_get', side_effect=[listing, detail]) as get:
        jobs = f.fetch_jobs('ignored', 'India')
        assert len(jobs) == 1
        assert jobs[0]['id'] == '65573aaf-b767-49b1-85b1-980807ffaa75'
        assert jobs[0]['location'] == 'Hyderabad, India'
        description, date = f.fetch_job_description(url)
        assert 'LangChain' in description and 'marketing' not in description
        assert date == ''
        assert f.fetch_jobs('second', 'India', start=1) == []
        assert get.call_count == 2
