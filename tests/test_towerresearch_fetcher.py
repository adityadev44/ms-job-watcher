from src import towerresearch_fetcher as f
def test_official_board_and_india_normalization_tokens():
    assert f._TOKEN == "towerresearchcapital"
    assert "gurgaon" in f._INDIA
    assert callable(f.fetch_jobs) and callable(f.fetch_job_description)
