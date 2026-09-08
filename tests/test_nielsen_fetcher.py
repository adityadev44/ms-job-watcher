from src import nielsen_fetcher as f
def test_verified_source_and_interface():
    assert "TheNielsenCompany" in f._BASE
    assert callable(f.fetch_jobs) and callable(f.fetch_job_description)
