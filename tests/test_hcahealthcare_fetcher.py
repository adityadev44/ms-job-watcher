import importlib
from unittest.mock import Mock
import pytest
from src import hcahealthcare_fetcher as fetcher

@pytest.fixture(autouse=True)
def reset():
    importlib.reload(fetcher)

def test_paginated_inline_jobs_have_real_links_and_locations(monkeypatch):
    monkeypatch.setattr(fetcher, "_ensure_live_context", lambda timeout: None)
    api = Mock(side_effect=[{"job_counts": 2, "data": [{"id": "a", "title": "Software Engineer", "tool_tip_locations": ["Hyderabad, Telangana, India"], "posted_on": 1791417600, "jd": "&lt;p&gt;C# and ASP.NET&lt;/p&gt;"}]}, {"data": [{"id": "b", "title": "Engineer", "country": "India", "jd": "<p>LangChain</p>"}]}])
    monkeypatch.setattr(fetcher, "_call_api", api)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    jobs = fetcher.fetch_jobs("python", "India", num=1)
    assert jobs[0]["location"] == "Hyderabad, Telangana, India"
    assert jobs[0]["application_url"] == "https://hcahr.darwinbox.in/ms/candidatev2/main/careers/jobDetails/a?from=all"
    assert fetcher.fetch_jobs("dotnet", "India", start=1)[0]["id"] == "b"
    assert fetcher.fetch_job_description(jobs[0]["application_url"])[0] == "C# and ASP.NET"
    assert api.call_count == 2
    assert api.call_args_list[1].args[1]["page"] == 2

def test_latched_failure_never_silently_returns_empty(monkeypatch):
    monkeypatch.setattr(fetcher, "_ensure_live_context", Mock(side_effect=fetcher.RateLimitError("down")))
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs("", "India")

def test_missing_location_is_not_fabricated_india():
    assert fetcher._location_from_job({}) == ""
