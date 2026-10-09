from src import rakuten_fetcher as f

def test_verified_source_pagination_and_original_date(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    calls = []
    def post(path, timeout, **kw):
        import json
        calls.append(json.loads(kw["files"]["filterCri"][1])["paginationStartNo"])
        return {"data": {"hasMoreData": len(calls) == 1, "data": [{"_source": {"id": 42, "jobTitle": "Software Engineer", "jobUrl": "software-engineer", "locationSeparatedbySlash": "Bengaluru, Karnataka, India", "createDate": "01-Oct-2026"}}]}}
    monkeypatch.setattr(f, "_post", post)
    jobs = f.fetch_jobs("", "India")
    assert calls == [0, 1]
    assert jobs[0]["posting_date"] == "2026-10-01"
    assert f.fetch_jobs("ignored", "India", start=1) == []

def test_detail_uses_full_description_and_decoded_company(monkeypatch):
    monkeypatch.setattr(f, "_desc_cache", {})
    calls = []
    def post(path, timeout, **kw):
        calls.append(kw["json"])
        return {"longDescription": "<p>C# .NET Core</p>", "createDate": "02-Oct-2026"}
    monkeypatch.setattr(f, "_post", post)
    assert f.fetch_job_description(f._BASE + "/jobview/role") == ("C# .NET Core", "2026-10-02")
    assert calls[0]["companyId"] == "15124"

def test_missing_search_data_caches_failure(monkeypatch):
    import pytest
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    calls = []
    def post(*args, **kw):
        calls.append(1)
        return {"data": None}
    monkeypatch.setattr(f, "_post", post)
    for key in ["", "python"]:
        with pytest.raises(f.RateLimitError):
            f.fetch_jobs(key, "India")
    assert len(calls) == 1
