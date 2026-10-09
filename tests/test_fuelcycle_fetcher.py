from src import fuelcycle_fetcher as f

def test_structured_country_and_secondary_india():
    assert f._india_location({"location": "India team", "address": {"postalAddress": {"addressCountry": "USA"}}}) == ""
    job = {"location": "US", "address": {"postalAddress": {"addressCountry": "USA"}}, "secondaryLocations": [
        {"location": "Navi Mumbai", "address": {"postalAddress": {"addressCountry": "IND", "addressLocality": "Navi Mumbai", "addressRegion": "Maharashtra"}}}
    ]}
    assert f._india_location(job) == "Navi Mumbai, Maharashtra, India"

def test_live_board_shape_and_description_cache(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    monkeypatch.setattr(f, "_descriptions", {})
    job = {"id": "uuid1", "title": "Software Engineer", "location": "Mumbai, India", "publishedAt": "2026-10-01T00:00:00Z", "descriptionHtml": "<p>Build C# services</p>", "jobUrl": "https://jobs.ashbyhq.com/fuel-cycle/uuid1"}
    calls = []
    def get(timeout):
        calls.append(1)
        return {"jobs": [job]}
    monkeypatch.setattr(f, "_get", get)
    assert f.fetch_jobs("", "India")[0]["id"] == "uuid1"
    assert f.fetch_job_description(job["jobUrl"]) == ("Build C# services", "2026-10-01")
    assert f.fetch_jobs("python", "India", start=1) == []
    assert len(calls) == 1

def test_failure_cached_without_silence(monkeypatch):
    import pytest
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    calls = []
    def get(timeout):
        calls.append(1)
        raise f.RateLimitError("offline")
    monkeypatch.setattr(f, "_get", get)
    for keyword in ["", "python"]:
        with pytest.raises(f.RateLimitError):
            f.fetch_jobs(keyword, "India")
    assert len(calls) == 1
