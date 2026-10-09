import importlib
from unittest.mock import Mock
import pytest
from src import providence_fetcher as fetcher

@pytest.fixture(autouse=True)
def reset():
    importlib.reload(fetcher)

def test_classic_rows_offset_country_and_detail(monkeypatch):
    listing = '<table><tr class="data-row"><td><a class="jobTitle-link" href="/job/Hyderabad-Engineer/123/">Software Engineer</a></td><td><span class="jobLocation">Hyderabad, Telangana, IN</span></td><td><span class="jobDate">Oct 8, 2026</span></td></tr></table>'
    responses = []
    for body in [listing, listing, '<meta itemprop="datePosted" content="Thu Oct 08 02:00:00 UTC 2026"><span class="jobdescription">C# and ASP.NET</span>']:
        response = Mock(text=body)
        response.raise_for_status.return_value = None
        responses.append(response)
    get = Mock(side_effect=responses)
    monkeypatch.setattr(fetcher.requests, "get", get)
    jobs = fetcher.fetch_jobs("python", "India")
    assert jobs[0]["location"] == "Hyderabad, Telangana, India"
    assert jobs[0]["posting_date"] == "2026-10-08"
    assert get.call_args_list[1].kwargs["params"]["startrow"] == 15
    assert fetcher.fetch_job_description(jobs[0]["application_url"]) == ("C# and ASP.NET", "2026-10-08")
    assert fetcher.fetch_jobs("dotnet", "India", start=1) == []

def test_failure_latch_remains_explicit(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    get = Mock(side_effect=fetcher.requests.ConnectionError("down"))
    monkeypatch.setattr(fetcher.requests, "get", get)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs("", "India")
    assert get.call_count == 3
