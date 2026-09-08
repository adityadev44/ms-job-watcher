"""Fetch Sun Life jobs from its official Workday careers tenant.

Live verified 2026-09-08. Workday searchText is used server-side; results are
then conservatively restricted to India from each posting's location text.
"""
from __future__ import annotations

import re
import time
import warnings
import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://sunlife.wd3.myworkdayjobs.com"
_SITE = "Experienced"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/sunlife/{_SITE}/jobs"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/sunlife/{_SITE}"
_PUBLIC_BASE = f"{_BASE_URL}/en-US/Experienced"
_PAGE_SIZE = 20
_FACETS = {"Location_Country": ["c4f78be1a8f14da0ab49ce1162348a5e"]}
_INDIA_TOKENS = ("india", "bengaluru", "bangalore", "hyderabad", "gurgaon", "gurugram", "noida", "mumbai", "delhi")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36", "Accept": "application/json", "Content-Type": "application/json"}

class RateLimitError(Exception):
    pass

def _request(method: str, url: str, *, timeout: int, **kwargs):
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                response = requests.request(method, url, headers=_HEADERS, timeout=timeout, verify=False, **kwargs)
            if response.status_code == 429:
                raise RateLimitError("Sun Life Workday rate limited")
            response.raise_for_status()
            return response
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Sun Life fetch failed: {exc}") from exc
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")

def _is_india(location: str) -> bool:
    value = location.lower()
    return bool(re.search(r"\bindia\b", value)) or any(token in value for token in _INDIA_TOKENS[1:])

def fetch_jobs(keyword: str, location: str, *, num: int = 20, start: int = 0, sort_by: str = "date", timeout: int = 20) -> list[dict[str, str]]:
    response = _request("POST", _SEARCH_URL, timeout=timeout, json={"appliedFacets": _FACETS, "limit": min(num, _PAGE_SIZE), "offset": start, "searchText": keyword})
    jobs = []
    for posting in response.json().get("jobPostings", []):
        loc = (posting.get("locationsText") or "").strip()
        if not _is_india(loc):
            continue
        if "india" not in loc.lower():
            loc += ", India"
        path = posting.get("externalPath") or ""
        job_id = path.rsplit("_", 1)[-1] if "_" in path else path
        if not job_id or not posting.get("title") or not path:
            continue
        jobs.append({"id": job_id, "title": posting["title"].strip(), "location": loc, "posting_date": "", "application_url": f"{_PUBLIC_BASE}{path}"})
    return jobs

def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    path = application_url.split(f"/en-US/Experienced", 1)[-1]
    response = _request("GET", f"{_DETAIL_BASE}{path}", timeout=timeout)
    data = response.json().get("jobPostingInfo", {})
    text = BeautifulSoup(data.get("jobDescription") or "", "html.parser").get_text(" ", strip=True)
    return text, data.get("startDate") or ""
