"""Uniphore's Workday fetcher: city-facet India scoping (locationCountry is
broken on this tenant), "N Locations" primary-location resolution via detail
lookup, and failure contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import uniphore_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache():
    fetcher._detail_cache.clear()
    yield
    fetcher._detail_cache.clear()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def search_response(job_postings, status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {"total": len(job_postings), "jobPostings": job_postings},
    )


def detail_response(location, description="<p>Build things.</p>",
                     additional_locations=None, start_date="2026-08-14", status_code=200):
    return Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {
            "jobPostingInfo": {
                "location": location,
                "additionalLocations": additional_locations or [],
                "jobDescription": description,
                "startDate": start_date,
            }
        },
    )


def raw_posting(job_id, title, locations_text="India - Bangalore", posted_on="Posted Today",
                 path=None):
    path = path or f"/job/India---Bangalore/{title.replace(' ', '-')}_{job_id}"
    return {
        "title": title,
        "externalPath": path,
        "timeType": "Full time",
        "locationsText": locations_text,
        "postedOn": posted_on,
        "bulletFields": [job_id],
    }


def test_fetch_jobs_resolves_primary_location_via_detail(monkeypatch):
    post = Mock(return_value=search_response([
        raw_posting("JR101144", "Senior Staff Software Engineer (SDET)", "2 Locations"),
    ]))
    get = Mock(return_value=detail_response("India - Bangalore", additional_locations=["India - Chennai"]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "JR101144"
    # Primary location resolved from detail, NOT the ambiguous "2 Locations"
    # placeholder and NOT falsely showing Chennai as the primary site.
    assert job["location"] == "India - Bangalore"


def test_fetch_jobs_sends_city_level_india_facet(monkeypatch):
    post = Mock(return_value=search_response([]))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    fetcher.fetch_jobs('security', 'India', num=20, start=0)
    _, kwargs = post.call_args
    body = kwargs['json']
    # locationCountry is broken on this tenant -- must use city-level WIDs.
    assert body["appliedFacets"] == {"locations": fetcher._INDIA_LOCATION_WIDS}
    assert body["searchText"] == "security"
    assert body["limit"] == 20
    assert body["offset"] == 0


def test_job_with_chennai_only_primary_location_kept_as_chennai(monkeypatch):
    post = Mock(return_value=search_response([
        raw_posting("JR2", "Backend Engineer", "India - Chennai"),
    ]))
    get = Mock(return_value=detail_response("India - Chennai"))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', 'India')
    assert jobs[0]["location"] == "India - Chennai"


def test_non_india_job_dropped_after_detail_resolution(monkeypatch):
    post = Mock(return_value=search_response([
        raw_posting("JR3", "Engineer", "USA - CA - Palo Alto"),
    ]))
    get = Mock(return_value=detail_response("USA - CA - Palo Alto"))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', '')
    assert jobs == []


def test_posting_without_bullet_fields_falls_back_to_external_path(monkeypatch):
    posting = raw_posting("JR4", "Engineer")
    posting["bulletFields"] = []
    posting["externalPath"] = "/job/India---Bangalore/Engineer_JR4"
    post = Mock(return_value=search_response([posting]))
    get = Mock(return_value=detail_response("India - Bangalore"))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', '')
    assert jobs[0]["id"] == "JR4"


def test_posting_without_title_is_skipped(monkeypatch):
    posting = raw_posting("JR5", "Engineer")
    posting["title"] = ""
    post = Mock(return_value=search_response([posting]))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    assert fetcher.fetch_jobs('', '') == []


def test_fetch_job_description_uses_cache_from_fetch_jobs(monkeypatch):
    post = Mock(return_value=search_response([
        raw_posting("JR6", "Engineer"),
    ]))
    get = Mock(return_value=detail_response(
        "India - Bangalore", description="<p>Build <b>scalable</b> systems.</p>",
    ))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', '')
    get.reset_mock()
    description, posting_date = fetcher.fetch_job_description(jobs[0]["application_url"])
    assert description == "Build scalable systems."
    assert posting_date == "2026-08-14"
    get.assert_not_called()  # served from cache, no extra HTTP call


def test_fetch_job_description_falls_back_to_direct_call_when_uncached():
    with pytest.MonkeyPatch.context() as mp:
        get = Mock(return_value=detail_response("India - Bangalore", description="<p>Direct fetch.</p>"))
        mp.setattr(fetcher.requests, 'get', get)
        description, posting_date = fetcher.fetch_job_description(
            "https://uniphore.wd503.myworkdayjobs.com/Uniphore/job/India---Bangalore/Engineer_JR9"
        )
        assert description == "Direct fetch."
        assert posting_date == "2026-08-14"


def test_rate_limit_raised_on_429(monkeypatch):
    resp = Mock(status_code=429)
    post = Mock(return_value=resp)
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    assert post.call_count == 3


def test_rate_limit_raised_on_persistent_connection_failure(monkeypatch):
    post = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'post', post)

    with pytest.raises(fetcher.RateLimitError):
        fetcher.fetch_jobs('', '')
    assert post.call_count == 3


def test_detail_lookup_failure_falls_back_to_list_location(monkeypatch):
    # If the per-job detail call fails, fetch_jobs should not crash the
    # whole page -- it falls back to the (possibly ambiguous) list-response
    # locationsText rather than dropping the job.
    post = Mock(return_value=search_response([
        raw_posting("JR7", "Engineer", "India - Bangalore"),
    ]))
    get = Mock(side_effect=requests.ConnectionError('down'))
    monkeypatch.setattr(fetcher.requests, 'post', post)
    monkeypatch.setattr(fetcher.requests, 'get', get)

    jobs = fetcher.fetch_jobs('', '')
    assert len(jobs) == 1
    assert jobs[0]["location"] == "India - Bangalore"
