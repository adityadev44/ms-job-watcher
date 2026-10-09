from unittest.mock import patch
from bs4 import BeautifulSoup
from src import voya_fetcher as f

def test_ajax_table_identity_and_scoped_description():
    f._jobs = None
    f._list_error = None
    f._descriptions.clear()
    soup = BeautifulSoup('<table class="jobtable"><tr><th>Req ID</th></tr><tr><td><a href="https://www.voyaindia.com/career-inner-page/36345/">36345</a></td><td>Senior Engineer - Software</td><td>India</td></tr></table>', 'html.parser')
    detail = BeautifulSoup('<nav>generic AI</nav><div class="jobdetails">C# .NET Core engineering</div>', 'html.parser')
    with patch.object(f, '_request', side_effect=[soup, detail]) as request:
        job = f.fetch_jobs('', 'India')[0]
        assert job['id'] == '36345'
        assert request.call_args.kwargs['data']['action'] == 'fetch_job_listings'
        assert f.fetch_job_description(job['application_url']) == ('C# .NET Core engineering', '')
