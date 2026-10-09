import importlib
from unittest.mock import Mock
import pytest
from src import cognite_fetcher as fetcher

@pytest.fixture(autouse=True)
def reset():
    importlib.reload(fetcher)

def test_whole_board_pagination_and_inline_description(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {"jobs": [
        {"id": 1, "title": "Software Engineer", "location": {"name": "Bengaluru, India"}, "absolute_url": "https://job-boards.greenhouse.io/cognite/jobs/1", "content": "&lt;p&gt;C# &amp; ASP.NET&lt;/p&gt;", "first_published": "2026-10-01T12:00:00Z"},
        {"id": 2, "title": "Software Engineer", "location": {"name": "Indianapolis"}, "absolute_url": "https://job-boards.greenhouse.io/cognite/jobs/2"},
        {"id": 3, "title": "Software Engineer", "location": {"name": "Hyderabad, India"}, "absolute_url": "https://job-boards.greenhouse.io/cognite/jobs/3"},
    ]}
    get = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, "get", get)
    jobs = fetcher.fetch_jobs("python", "India", num=1)
    assert [j["id"] for j in jobs] == ["1"]
    assert fetcher.fetch_jobs("dotnet", "India", start=1)[0]["id"] == "3"
    assert fetcher.fetch_job_description(jobs[0]["application_url"]) == ("C# & ASP.NET", "2026-10-01")
    assert get.call_count == 1

def test_failed_board_is_visible_on_subsequent_calls(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    get = Mock(side_effect=fetcher.requests.ConnectionError("down"))
    monkeypatch.setattr(fetcher.requests, "get", get)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs("", "India")
    assert get.call_count == 3

def test_closed_or_invalid_detail_does_not_return_empty(monkeypatch):
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description("https://example.com/invalid")
