from src import abcfitness_fetcher as f

def test_india_facet_and_multilocation_and_wraparound(monkeypatch):
    monkeypatch.setattr(f, "_first_ids", {})
    calls = []
    def request(method, url, timeout, **kw):
        calls.append(kw["json"])
        return {"jobPostings": [{"title": "Software Engineer", "externalPath": "/job/Office/Software-Engineer_REQ-42", "locationsText": "2 Locations", "postedOn": "Posted Yesterday", "bulletFields": ["REQ -42"]}]}
    monkeypatch.setattr(f, "_request", request)
    jobs = f.fetch_jobs("python", "India", num=100)
    assert calls[0]["limit"] == 20
    assert calls[0]["appliedFacets"] == {"locations": [f._FACET]}
    assert jobs[0]["id"] == "REQ-42"
    assert "India" in jobs[0]["location"]
    assert f.fetch_jobs("python", "India", start=20) == []

def test_workday_description_and_date(monkeypatch):
    monkeypatch.setattr(f, "_desc_cache", {})
    calls = []
    def request(method, url, timeout, **kw):
        calls.append(url)
        return {"jobPostingInfo": {"jobDescription": "<p>Build .NET services</p>", "startDate": "2026-10-01"}}
    monkeypatch.setattr(f, "_request", request)
    url = f"{f._BASE}/{f._SITE}/job/India/Developer_REQ-42"
    assert f.fetch_job_description(url) == ("Build .NET services", "2026-10-01")
    assert calls == [f._API + "/job/India/Developer_REQ-42"]
    assert f._date("Posted 30+ Days Ago") == ""
