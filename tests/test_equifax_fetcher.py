from unittest.mock import Mock, patch

from src import equifax_fetcher as fetcher


def test_fetch_jobs_keeps_only_india_and_normalizes_location():
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {"jobPostings": [
        {"title": "Senior Software Engineer", "externalPath": "/job/Bangalore/Role_R1", "locationsText": "Bangalore"},
        {"title": "Engineer", "externalPath": "/job/London/Role_R2", "locationsText": "London"},
    ]}
    with patch.object(fetcher.requests, "request", return_value=response):
        jobs = fetcher.fetch_jobs("engineer", "India", num=20, start=0)
    assert [job["id"] for job in jobs] == ["R1"]
    assert jobs[0]["location"] == "Bangalore, India"


def test_fetch_job_description_uses_workday_json():
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {"jobPostingInfo": {"jobDescription": "<p>C# and ASP.NET</p>", "startDate": "2026-09-08"}}
    with patch.object(fetcher.requests, "request", return_value=response):
        description, date = fetcher.fetch_job_description("https://equifax.wd5.myworkdayjobs.com/en-US/External/job/x_R1")
    assert description == "C# and ASP.NET"
    assert date == "2026-09-08"

