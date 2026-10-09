import pytest
from src import redwoodsoftware_fetcher as f


def test_india_pagination_description_and_date(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_failure", None)
    monkeypatch.setattr(f, "_descriptions", {})
    calls = []
    def get(url, timeout):
        calls.append(url)
        return {"jobs": [
            {"id": 1, "title": "Software Engineer", "location": {"name": "Hyderabad, India"}, "content": "&amp;lt;p&amp;gt;C# .NET&amp;lt;/p&amp;gt;", "first_published": "2026-10-01T00:00:00Z"},
            {"id": 2, "title": "Developer", "location": {"name": "Indianapolis, US"}},
            {"id": 3, "title": "Developer", "location": {"name": "Pune, India"}, "updated_at": "2026-10-09"}]}
    monkeypatch.setattr(f, "_get", get)
    jobs = f.fetch_jobs("", "India", num=1)
    assert jobs[0]["id"] == "1"
    assert f.fetch_job_description(jobs[0]["application_url"]) == ("C# .NET", "2026-10-01")
    assert f.fetch_jobs("other", "India", start=1)[0]["posting_date"] == ""
    assert f.fetch_jobs("", "India", start=2) == []
    assert len(calls) == 1


def test_failure_remains_visible_without_retry_storm(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_failure", None)
    calls = []
    def fail(url, timeout):
        calls.append(url)
        raise f.RateLimitError("offline")
    monkeypatch.setattr(f, "_get", fail)
    for _ in range(2):
        with pytest.raises(f.RateLimitError):
            f.fetch_jobs("", "India")
    assert len(calls) == 1
