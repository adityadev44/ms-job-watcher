from unittest.mock import Mock, patch
from src import wolterskluwer_fetcher as fetcher

def test_fetch_jobs_keeps_india_prefixed_locations():
    r=Mock(status_code=200); r.raise_for_status.return_value=None
    r.json.return_value={"jobPostings":[
      {"title":"Product Software Engineer","externalPath":"/job/x_R1","locationsText":"IND - Gurgaon"},
      {"title":"Engineer","externalPath":"/job/x_R2","locationsText":"USA - NY"}]}
    with patch.object(fetcher.requests,"request",return_value=r):
        jobs=fetcher.fetch_jobs("engineer","India")
    assert [j["id"] for j in jobs]==["R1"]
    assert jobs[0]["location"].endswith(", India")

def test_description():
    r=Mock(status_code=200); r.raise_for_status.return_value=None
    r.json.return_value={"jobPostingInfo":{"jobDescription":"<p>C# .NET</p>","startDate":"2026-09-08"}}
    with patch.object(fetcher.requests,"request",return_value=r):
        assert fetcher.fetch_job_description("https://wk.wd3.myworkdayjobs.com/en-US/External/job/x_R1")==("C# .NET","2026-09-08")

