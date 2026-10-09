from bs4 import BeautifulSoup
from src import meltwater_fetcher as f

def test_full_pool_paging_and_multilocation_india(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    calls = []
    def get(url, timeout):
        calls.append(url)
        if url == f._BASE:
            return BeautifulSoup("""
            <table><tr><td class="jv-job-list-name"><a href="/meltwater/job/one">Software Engineer</a></td><td class="jv-job-list-location">Hyderabad, India</td></tr>
            <tr><td class="jv-job-list-name"><a href="/meltwater/job/two">Engineer</a></td><td class="jv-job-list-location">London, UK</td></tr>
            <tr><td class="jv-job-list-name"><a href="/meltwater/job/three">AI Engineer</a></td><td class="jv-job-list-location">2 Locations</td></tr></table>
            """, "html.parser")
        return BeautifulSoup('<div class="jv-job-detail-meta">Engineering Hyderabad, India; London, UK</div>', "html.parser")
    monkeypatch.setattr(f, "_get", get)
    assert f.fetch_jobs("", "India", num=1)[0]["id"] == "one"
    assert f.fetch_jobs("ignored", "India", start=1)[0]["id"] == "three"
    assert f.fetch_jobs("", "India", start=2) == []
    assert len(calls) == 2

def test_missing_detail_fails_closed(monkeypatch):
    import pytest
    monkeypatch.setattr(f, "_desc_cache", {})
    monkeypatch.setattr(f, "_get", lambda *args: BeautifulSoup("<div>No job</div>", "html.parser"))
    with pytest.raises(f.RateLimitError):
        f.fetch_job_description(f._BASE + "/job/closed")

def test_cache_failure_remains_explicit(monkeypatch):
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
