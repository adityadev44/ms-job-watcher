from src import aqr_fetcher as f
def test_official_india_board_and_interface():
    assert f._TOKEN == "india"
    assert callable(f.fetch_jobs) and callable(f.fetch_job_description)
    assert "greenhouse" in f._URL
