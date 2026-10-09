from bs4 import BeautifulSoup
from src import hyland_fetcher as f


def test_india_subset_can_follow_foreign_only_page(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_failure", None)
    calls = []
    def get(url, timeout):
        calls.append(url)
        if 'pr=0' in url:
            body = '<a href="?pr=1">Next</a>'
            ident, loc = 1, 'Remote - U.S.'
        else:
            body = ''
            ident, loc = 2, 'Hyderabad India Office'
        return BeautifulSoup(body + f'''<div class="iCIMS_JobsTable"><div class="row"><a href="/jobs/{ident}/developer/job"><h3>Developer</h3></a><dl><dt>Job Locations</dt><dd>{loc}</dd></dl></div></div>''', 'html.parser')
    monkeypatch.setattr(f, "_get", get)
    jobs = f.fetch_jobs("", "India")
    assert [j['id'] for j in jobs] == ['2']
    assert jobs[0]['location'] == 'Hyderabad India Office'
    assert len(calls) == 2
    assert f.fetch_jobs("different", "India", start=1) == []


def test_full_description_date(monkeypatch):
    monkeypatch.setattr(f, "_details", {})
    monkeypatch.setattr(f, "_get", lambda *args: BeautifulSoup('<div class="iCIMS_JobContent">C# .NET</div><script type="application/ld+json">{"datePosted":"2026-09-20"}</script>', 'html.parser'))
    assert f.fetch_job_description('https://example.com/jobs/1') == ('C# .NET', '2026-09-20')
