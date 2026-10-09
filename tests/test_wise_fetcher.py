from unittest.mock import Mock
from src import wise_fetcher as fetcher

def test_country_filter_offset_and_location(monkeypatch):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"content": [
        {"id": "1", "name": "Software Engineer", "location": {"city": "Hyderabad", "country": "in"}, "releasedDate": "2026-10-01T00:00:00Z"},
        {"id": "2", "name": "Software Engineer", "location": {"city": "London", "country": "gb"}},
    ]}
    get = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, "get", get)
    jobs = fetcher.fetch_jobs("python", "India", start=20, num=200)
    assert [j["id"] for j in jobs] == ["1"]
    assert jobs[0]["location"] == "Hyderabad, India"
    assert get.call_args.kwargs["params"] == {"country": "in", "limit": 100, "offset": 20}

def test_full_description_sections_exclude_company_boilerplate(monkeypatch):
    fetcher._desc_cache.clear()
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"jobAd": {"sections": {"companyDescription": {"text": "C# boilerplate"}, "jobDescription": {"text": "<p>Build Java</p>"}, "qualifications": {"text": "Python"}}}}
    monkeypatch.setattr(fetcher.requests, "get", Mock(return_value=response))
    assert fetcher.fetch_job_description("https://jobs.smartrecruiters.com/Wise/123-role")[0] == "Build Java Python"
