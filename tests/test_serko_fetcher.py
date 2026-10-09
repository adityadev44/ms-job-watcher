from unittest.mock import patch
from bs4 import BeautifulSoup
from src import serko_fetcher as f

def test_india_cards_only_and_complete_description_sections():
    f._jobs = None
    f._descriptions.clear()
    listing = BeautifulSoup('<a class="job_opening-wrapper" href="/job-listing/data-engineer-bengaluru-india"><div class="job_opening-content"><div>Data Engineer</div><div>Bengaluru, India</div></div></a><a class="job_opening-wrapper" href="/job-listing/us"><div class="job_opening-content"><div>Engineer</div><div>Seattle, USA</div></div></a>', 'html.parser')
    detail = BeautifulSoup('<nav>marketing .NET</nav><div class="job_opening-content-wrapper"><div class="job_opening-info">Intro</div></div><div class="job_opening-content-wrapper"><div class="job_opening-info">LangChain requirements</div></div>', 'html.parser')
    with patch.object(f, '_get', side_effect=[listing, detail]):
        jobs = f.fetch_jobs('', 'India')
        assert len(jobs) == 1
        assert f.fetch_job_description(jobs[0]['application_url']) == ('Intro LangChain requirements', '')
