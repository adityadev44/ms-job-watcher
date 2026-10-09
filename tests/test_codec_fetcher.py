from src import codec_fetcher as f

def test_locations_and_all_description_sections():
    assert f._location({"location": {"name": "Belfast"}}) == ""
    assert f._location({"location": {"name": "US - Indiana"}}) == ""
    assert f._location({"location": {"city": "Hyderabad", "name": "Hyderabad"}}) == "Hyderabad, India"
    assert ".NET" in f._description({"description": "<p>Build software</p>", "key_responsibilities": "Deliver services", "skills_knowledge_expertise": "<p>.NET</p>"})

def test_paging_and_inline_lookup(monkeypatch):
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    monkeypatch.setattr(f, "_descriptions", {})
    url = "https://codec.pinpointhq.com/en/postings/uuid"
    monkeypatch.setattr(f, "_get", lambda timeout: {"data": [{"id": "42", "url": url, "title": "Software Engineer", "location": {"name": "Mumbai, India"}, "skills_knowledge_expertise": "<p>C#</p>"}]})
    assert f.fetch_jobs("", "India")[0]["id"] == "42"
    assert f.fetch_job_description(url)[0].strip() == "C#"
    assert f.fetch_jobs("", "India", start=1) == []

def test_cached_failure_stays_explicit(monkeypatch):
    import pytest
    monkeypatch.setattr(f, "_jobs", None)
    monkeypatch.setattr(f, "_cache_error", None)
    calls = []
    def get(timeout):
        calls.append(1)
        raise f.RateLimitError("offline")
    monkeypatch.setattr(f, "_get", get)
    for keyword in ["", "python"]:
        with pytest.raises(f.RateLimitError):
            f.fetch_jobs(keyword, "India")
    assert len(calls) == 1
