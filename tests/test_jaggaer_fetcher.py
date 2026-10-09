from bs4 import BeautifulSoup
from src import jaggaer_fetcher as f


def test_structured_country_and_pagination(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_failure", None)
    def get(url, timeout):
        return BeautifulSoup('''<div class="iCIMS_JobsTable">
        <div class="row"><div class="header left">Job Locations IN-Hyderabad</div><a href="/jobs/12/developer/job"><h3>Developer</h3></a></div>
        <div class="row"><div class="header left">Job Locations US-IN-Indianapolis</div><a href="/jobs/13/developer/job"><h3>Developer</h3></a></div></div>''', 'html.parser')
    monkeypatch.setattr(f, "_get", get)
    jobs = f.fetch_jobs("", "India", num=1)
    assert len(jobs) == 1 and jobs[0]["location"] == "Hyderabad, India"
    assert jobs[0]["id"] == "12"
    assert f.fetch_jobs("other", "India", start=1) == []


def test_full_description_and_original_date(monkeypatch):
    monkeypatch.setattr(f, "_details", {})
    monkeypatch.setattr(f, "_get", lambda *args: BeautifulSoup('''<div class="iCIMS_JobContent">Required C# .NET</div><script type="application/ld+json">{"datePosted":"2026-10-01T00:00:00Z"}</script>''', 'html.parser'))
    assert f.fetch_job_description("https://example.com/jobs/12/") == ("Required C# .NET", "2026-10-01")
