from unittest.mock import patch
from bs4 import BeautifulSoup
from src import nemetschek_fetcher as f

def test_indien_normalization_and_pagination():
    f._jobs = None
    first = BeautifulSoup('<a class="hoverelemjob" href="https://careers.nemetschek.com/job-invite/123"><div class="content__grid-images_textcontent_content">Software Engineer</div><div class="text-left">Indien</div></a><li class="pager__item--next"><a href="?field_saf_country_target_id=772&amp;page=1">Next</a></li>', 'html.parser')
    second = BeautifulSoup('', 'html.parser')
    with patch.object(f, '_get', side_effect=[first, second]) as get:
        jobs = f.fetch_jobs('', 'India')
        assert len(jobs) == 1 and jobs[0]['location'] == 'India'
        assert jobs[0]['id'] == '123'
        assert get.call_count == 2
        assert 'page=1' in get.call_args.args[0]

def test_genuine_empty_board_returns_zero():
    f._jobs = None
    with patch.object(f, '_get', return_value=BeautifulSoup('<p>No vacancies</p>', 'html.parser')):
        assert f.fetch_jobs('', 'India') == []
