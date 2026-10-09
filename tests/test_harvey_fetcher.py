import importlib
from unittest.mock import Mock
import pytest
from src import harvey_fetcher as fetcher

@pytest.fixture(autouse=True)
def reset():
    importlib.reload(fetcher)

def test_country_secondary_location_inline_body_and_pagination(monkeypatch):
    india = {"location": "Bengaluru", "address": {"postalAddress": {"addressCountry": "India"}}}
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"jobs": [
        dict(india, id="a", title="Software Engineer", jobUrl="https://jobs.ashbyhq.com/harvey/a", descriptionPlain="Build LangChain", publishedAt="2026-10-01T00:00:00Z"),
        {"id": "b", "title": "Engineer", "location": "London", "secondaryLocations": [india], "jobUrl": "https://jobs.ashbyhq.com/harvey/b", "descriptionHtml": "<p>C#</p>"},
        {"id": "c", "title": "Engineer", "location": "Indianapolis", "jobUrl": "https://jobs.ashbyhq.com/harvey/c"},
        dict(india, id="d", title="Engineer", jobUrl="x", isListed=False),
    ]}
    get = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, "get", get)
    jobs = fetcher.fetch_jobs("", "India", num=1)
    assert jobs[0]["location"] == "Bengaluru, India"
    assert fetcher.fetch_jobs("python", "India", start=1)[0]["id"] == "b"
    assert fetcher.fetch_job_description(jobs[0]["application_url"]) == ("Build LangChain", "2026-10-01")
    assert get.call_count == 1
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description("https://jobs.ashbyhq.com/harvey/closed")

def test_error_latch_stays_visible(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    get = Mock(side_effect=fetcher.requests.ConnectionError("down"))
    monkeypatch.setattr(fetcher.requests, "get", get)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs("", "India")
    assert get.call_count == 3
