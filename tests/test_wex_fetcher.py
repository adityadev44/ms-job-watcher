import importlib
from unittest.mock import Mock
from src import wex_fetcher as fetcher

def test_verified_country_facet_and_wraparound(monkeypatch):
    importlib.reload(fetcher)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"jobPostings": [
        {"title": "Software Engineer", "externalPath": "/job/India---Bangalore/Role_R1", "locationsText": "India - Bangalore", "bulletFields": ["R1"]},
        {"title": "Engineer", "externalPath": "/job/London/Role_R2", "locationsText": "London", "bulletFields": ["R2"]},
    ]}
    post = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, "post", post)
    jobs = fetcher.fetch_jobs("software", "India", num=50)
    assert [j["id"] for j in jobs] == ["R1"]
    assert post.call_args.kwargs["json"]["appliedFacets"] == {"LocationCountry": ["c4f78be1a8f14da0ab49ce1162348a5e"]}
    assert post.call_args.kwargs["json"]["limit"] == 20
    assert fetcher.fetch_jobs("software", "India", start=20) == []

def test_description_uses_exact_cxs_path(monkeypatch):
    fetcher._desc_cache.clear()
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"jobPostingInfo": {"jobDescription": "<p>C# and ASP.NET</p>", "startDate": "2026-10-01"}}
    get = Mock(return_value=response)
    monkeypatch.setattr(fetcher.requests, "get", get)
    url = "https://wexinc.wd5.myworkdayjobs.com/WEXInc/job/India---Bangalore/Role_R1"
    assert fetcher.fetch_job_description(url) == ("C# and ASP.NET", "2026-10-01")
    assert get.call_args.args[0] == "https://wexinc.wd5.myworkdayjobs.com/wday/cxs/wexinc/WEXInc/job/India---Bangalore/Role_R1"

def test_ambiguous_location_resolves_from_structured_detail(monkeypatch):
    importlib.reload(fetcher)
    search = Mock()
    search.raise_for_status.return_value = None
    search.json.return_value = {"jobPostings": [{"title": "Software Engineer", "externalPath": "/job/Multi/Role_R3", "locationsText": "2 Locations", "bulletFields": ["R3"]}]}
    detail = Mock()
    detail.raise_for_status.return_value = None
    detail.json.return_value = {"jobPostingInfo": {"jobDescription": "<p>C#</p>", "location": "London", "additionalLocations": ["India - Hyderabad"], "country": {"descriptor": "United Kingdom"}}}
    monkeypatch.setattr(fetcher.requests, "post", Mock(return_value=search))
    monkeypatch.setattr(fetcher.requests, "get", Mock(return_value=detail))
    assert fetcher.fetch_jobs("software", "India")[0]["location"] == "India - Hyderabad"

def test_country_boundary_relative_bound_and_keyword_watermarks(monkeypatch):
    importlib.reload(fetcher)
    assert fetcher._normalize_location("Indianapolis") == "Indianapolis"
    assert fetcher._parse_posted_on("Posted 30+ Days Ago") == ""
    responses = []
    for job_id in ["R1", "R2", "R1"]:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"jobPostings": [{"title": "Software Engineer", "externalPath": "/job/India/Role_" + job_id, "locationsText": "India - Hyderabad", "bulletFields": [job_id]}]}
        responses.append(response)
    monkeypatch.setattr(fetcher.requests, "post", Mock(side_effect=responses))
    assert fetcher.fetch_jobs("software", "India")[0]["id"] == "R1"
    assert fetcher.fetch_jobs("python", "India")[0]["id"] == "R2"
    assert fetcher.fetch_jobs("software", "India", start=20) == []
