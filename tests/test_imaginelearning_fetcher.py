import importlib
from unittest.mock import Mock
import pytest
from src import imaginelearning_fetcher as fetcher

@pytest.fixture(autouse=True)
def reset():
    importlib.reload(fetcher)

def test_span_theme_country_normalization_wrapping_and_detail(monkeypatch):
    listing = '<ul class="jv-search-list"><a href="/imagine-learning/job/a"><span class="jv-job-list-name">Software Engineer</span><span class="jv-job-list-location">Bengaluru, Karnataka</span></a><a href="/imagine-learning/job/b"><span class="jv-job-list-name">Engineer</span><span class="jv-job-list-location">Indianapolis</span></a></ul>'
    responses = []
    for body in [listing, listing, '<div class="jv-job-detail-description">C# and .NET</div>']:
        response = Mock(text=body)
        response.raise_for_status.return_value = None
        responses.append(response)
    get = Mock(side_effect=responses)
    monkeypatch.setattr(fetcher.requests, "get", get)
    jobs = fetcher.fetch_jobs("", "India")
    assert [j["id"] for j in jobs] == ["a"]
    assert jobs[0]["location"] == "Bengaluru, Karnataka, India"
    assert fetcher.fetch_jobs("python", "India", start=1) == []
    assert fetcher.fetch_job_description(jobs[0]["application_url"]) == ("C# and .NET", "")
    assert get.call_count == 3

def test_description_layout_failure_is_explicit(monkeypatch):
    response = Mock(text="<html>not a job</html>")
    response.raise_for_status.return_value = None
    monkeypatch.setattr(fetcher.requests, "get", Mock(return_value=response))
    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_job_description("https://jobs.jobvite.com/imagine-learning/job/x")
