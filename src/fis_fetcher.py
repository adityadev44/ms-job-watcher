"""
FIS Global job fetcher — Workday CXS REST API (fis.wd5.myworkdayjobs.com).

Plain HTTP POST/GET, no Playwright needed.

India filter: server-side via Workday location facets (all current India office
IDs are listed in _INDIA_LOCATION_IDS).  Location strings are normalised from
Workday codes ("IND PUNE FL7") to readable city names ("Pune, India") so the
shared matcher's is_india_job and exclude_locations checks work correctly.

Pagination: Workday uses limit/offset; all returned jobs are India jobs so
page length advances correctly.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://fis.wd5.myworkdayjobs.com"
_SITE = "SearchJobs"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/fis/{_SITE}/jobs"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/fis/{_SITE}"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"

_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# All current FIS India office location IDs from Workday facets.
# Chennai and Pune are included — the shared matcher applies exclude_locations.
_INDIA_LOCATION_IDS = [
    "a2088ec533f41026992d77049749a891",  # IND BNGL  (Bangalore)
    "a2088ec533f41026992e5a04dbb3a995",  # IND CHNN  (Chennai 1)
    "abb80038590d01916fd231e4f2012c6b",  # IND CHNN  (Chennai 2)
    "a2088ec533f41026992e481e6765a97c",  # IND HRYN  (Gurugram)
    "bbc5678ae20c0100b02e92a2fe4b0000",  # IND HYDB  (Hyderabad)
    "a2088ec533f41026992e20cf8ae5a945",  # IND MUMB  (Mumbai)
    "191f7c57cf8c1001bb496d0089360000",  # IND NOID  (Noida)
    "f3837ff2985c015f5cf3d7635118d0a2",  # IND PUNE FL2
    "a2088ec533f41026992fbf03338faaad",  # IND PUNE FL7
]

# Map Workday city abbreviations → readable city names.
_CITY_CODE_MAP = {
    "BNGL": "Bangalore",
    "CHNN": "Chennai",
    "HRYN": "Gurugram",
    "HYDB": "Hyderabad",
    "MUMB": "Mumbai",
    "NOID": "Noida",
    "PUNE": "Pune",
}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _normalise_location(loc_text: str) -> str:
    """Convert "IND PUNE FL7" → "Pune, India" using known city codes."""
    parts = loc_text.split()
    if len(parts) >= 2:
        city = _CITY_CODE_MAP.get(parts[1], parts[1])
    else:
        city = loc_text
    return f"{city}, India"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    """Fetch one page of FIS India jobs matching keyword.

    Uses Workday location facets to filter to India server-side, so all
    returned jobs are India jobs and pagination advances correctly.
    """
    body = {
        "appliedFacets": {"locations": _INDIA_LOCATION_IDS},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }

    for attempt in range(3):
        try:
            r = requests.post(
                _SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout
            )
            if r.status_code == 429:
                raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"FIS search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    data = r.json()
    postings = data.get("jobPostings", [])

    jobs = []
    for j in postings:
        loc_text = j.get("locationsText", "")
        ext_path = j.get("externalPath", "")

        # Skip multi-location entries ("2 Locations" etc.)
        if "Locations" in loc_text:
            continue

        # Extract job ID (JR number) from the externalPath
        id_m = re.search(r"(JR\d+)", ext_path)
        if not id_m:
            continue
        job_id = id_m.group(1)

        title = j.get("title", "").strip()
        location_str = _normalise_location(loc_text)
        application_url = f"{_JOB_BASE}{ext_path}"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": "",  # filled in by fetch_job_description
            "application_url": application_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch full description and posting date for a single FIS job.

    Converts the human-facing URL to the Workday CXS API path.
    Returns (description_text, posting_date_YYYY-MM-DD).
    """
    # application_url = https://fis.wd5.myworkdayjobs.com/SearchJobs/job/IND-.../Title_JR123
    # API URL         = https://fis.wd5.myworkdayjobs.com/wday/cxs/fis/SearchJobs/job/...
    api_url = application_url.replace(f"{_BASE_URL}/{_SITE}", _DETAIL_BASE)

    for attempt in range(3):
        try:
            r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on detail for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    data = r.json()
    jpi = data.get("jobPostingInfo", {})

    posting_date = jpi.get("startDate", "")   # already YYYY-MM-DD
    desc_html = jpi.get("jobDescription", "")
    description = _strip_html(desc_html)

    return description, posting_date
