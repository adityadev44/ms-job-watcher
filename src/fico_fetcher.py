"""Fetches FICO (Fair Isaac Corporation) job listings via the Workday public REST API.

FICO's ATS is Workday, hosted at fico.wd1.myworkdayjobs.com/External.
The Workday CXS endpoint accepts plain POST requests -- no browser needed.

India has no reliable server-side country facet on this tenant: applying
`locationCountry` with the standard cross-tenant India WID
(c4f78be1a8f14da0ab49ce1162348a5e -- same GUID reused across Wells
Fargo/Fidelity/Citi/Northern Trust/MUFG/Novartis) returns the full ~88-job
global pool unfiltered (verified 2026-09-08, same "broken locationCountry
facet" class documented for Micron/Verizon/Lowe's).

Instead, the tenant's `locations` (city-level) facet works reliably.
Verified 2026-09-08: applying the 3 known India location WIDs (Bangalore,
Mumbai, Work from Home India) returns exactly 16 jobs, all genuinely
India-based (cross-checked against unfiltered searchText="India", which
coincidentally also returns 16 identical results). All 16 are in
Bangalore, Mumbai, or "Work from Home, India" -- none in Chennai, Pune, or
any other excluded city.

`searchText` is genuinely applied server-side on this tenant (34/88 for
"software engineer" vs. the full 88 with no search text), and combines
correctly with the locations facet.

`limit` > 20 returns a hard HTTP 400 (same behavior as Northern Trust) --
capped defensively.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://fico.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/fico/External/jobs"
_JOB_BASE = f"{_BASE_URL}/External"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/fico/External"
_MAX_PAGE_SIZE = 20

# India `locations` facet WIDs for FICO's Workday tenant, discovered from a
# live unfiltered search's own facet response on 2026-09-08. This tenant has
# no working country-level facet, so city-level WIDs are used instead.
_INDIA_LOCATION_WIDS = [
    "a46e175d937b106cf81e3390fa9bfb99",  # Bangalore, India
    "a46e175d937b106cf81e3788bb7afb9e",  # Mumbai, India
    "a46e175d937b106cf81d9489b810facc",  # Work from Home, India
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/External",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper -- Workday returns relative strings like "Posted 3 Days Ago"
# ---------------------------------------------------------------------------

def _parse_posted_on(posted_on: str) -> str:
    """Convert Workday's relative date string to YYYY-MM-DD."""
    if not posted_on:
        return ""
    s = posted_on.strip().lower()
    today = date.today()

    if "today" in s:
        return today.strftime("%Y-%m-%d")

    if "yesterday" in s:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    if "30+" in s:
        return (today - timedelta(days=30)).strftime("%Y-%m-%d")

    m = re.search(r"(\d+)\s+day", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")

    m = re.search(r"(\d+)\s+week", s)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")

    m = re.search(r"(\d+)\s+month", s)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")

    return ""


_INDIA_RE = re.compile(r"\bindia\b", re.IGNORECASE)


def _normalize_location(loc_text: str) -> str:
    """Normalize a Workday locationsText for a job already scoped to India
    by the `locations` facet. "2 Locations"-style ambiguous strings (a
    multi-site req where at least one site matched the India facet) are
    relabelled "India" -- same safety-net pattern used for Maersk/SimCorp.
    """
    loc = (loc_text or "").strip()
    if not loc or not _INDIA_RE.search(loc):
        return "India"
    return loc


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _MAX_PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    body = {
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
        "limit": min(num, _MAX_PAGE_SIZE),
        "offset": start,
        "searchText": keyword or "",
    }

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    json=body,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("FICO Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"FICO fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(\d+)$", field.strip())
            if m:
                job_id = m.group(1)
                break
        if not job_id:
            m = re.search(r"_(\d+(?:-\d+)?)$", external_path)
            if m:
                job_id = m.group(1)
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = _normalize_location(p.get("locationsText", ""))

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    The application_url (HTML page) is transformed to the JSON API path.
    Returns (description_text, posting_date).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/External/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    api_url,
                    headers=_HEADERS,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("FICO description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"FICO description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
