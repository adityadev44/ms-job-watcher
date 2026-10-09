from src import billtrust_fetcher as f

def test_india_filter_pagination_and_encoded_description(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    monkeypatch.setattr(f, "_descriptions", {})
    calls = []
    def get(url, timeout):
        calls.append(url)
        return {"jobs": [
            {"id": 1, "title": "Software Engineer", "location": {"name": "Hyderabad, India"}, "content": "&amp;lt;p&amp;gt;C# .NET&amp;lt;/p&amp;gt;", "first_published": "2026-10-01T12:00:00Z"},
            {"id": 2, "title": "Software Engineer", "location": {"name": "Indianapolis, US"}, "content": "C#"},
            {"id": 3, "title": "Developer", "location": {"name": "Pune, India"}, "content": "Python"}
        ]}
    monkeypatch.setattr(f, "_get", get)
    jobs = f.fetch_jobs("", "India", num=1)
    assert jobs[0]["id"] == "1"
    assert jobs[0]["posting_date"] == "2026-10-01"
    assert f.fetch_job_description(jobs[0]["application_url"]) == ("C# .NET", "2026-10-01")
    assert f.fetch_jobs("different", "India", start=1)[0]["id"] == "3"
    assert f.fetch_jobs("", "India", start=2) == []
    assert len(calls) == 1

def test_branded_query_id_and_date_not_updated_at():
    assert f._id("https://example.com/openings?gh_jid=123") == "123"
    assert f._date({"updated_at": "2026-10-09T00:00:00Z"}) == ""

def test_persistent_http_failure_is_explicit(monkeypatch):
    import pytest
    import requests
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(f.requests, "get", get)
    monkeypatch.setattr(f.time, "sleep", lambda delay: None)
    with pytest.raises(f.RateLimitError):
        f._get(f._BASE, 1)
    assert len(calls) == 3

def test_failed_cache_remains_explicit_without_retry_storm(monkeypatch):
    import pytest
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    calls = []
    def get(*args):
        calls.append(1)
        raise f.RateLimitError("offline")
    monkeypatch.setattr(f, "_get", get)
    for keyword in ["", "python"]:
        with pytest.raises(f.RateLimitError):
            f.fetch_jobs(keyword, "India")
    assert len(calls) == 1
