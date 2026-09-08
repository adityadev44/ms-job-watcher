from src import smbc_fetcher as f
def test_verified_successfactors_board_and_interface():
    assert f._BASE == "https://careerasia.smbc.co.jp"
    assert callable(f.fetch_jobs) and callable(f.fetch_job_description)
