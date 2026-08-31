"""Fetches Nasdaq job listings via the Workday public REST API.

Nasdaq's ATS is Workday, hosted at nasdaq.wd1.myworkdayjobs.com (tenant
"nasdaq", site "Global_External_Site"). Confirmed live 2026-08-31:
POST to /wday/cxs/nasdaq/Global_External_Site/jobs returns real jobPostings,
and unlike most fetchers in this repo `searchText` genuinely narrows results
server-side (33 total India jobs -> 24 for "software engineer" -> 25 for
"engineer") -- keyword filtering is real here, not a no-op.

India is filtered server-side via a "Location_Country" facet (capitalised,
same key as Marsh McLennan/State Street -- NOT the lowercase "locationCountry"
key most Workday tenants use; sending the lowercase key 400s). India WID
c4f78be1a8f14da0ab49ce1162348a5e is the same cross-tenant India GUID reused
across Fidelity/Wells Fargo/Citi/Northern Trust/MUFG/Accenture. Only ~33
total India jobs -- a small, direct-employer footprint, not a services shop.

Two known Workday quirks both confirmed live on this tenant:
- Page size hard-capped at 20 (like Northern Trust): `limit` above 20 (21+)
  returns HTTP 400. fetch_jobs clamps `num` defensively.
- Pagination wraps around past the real result count instead of returning an
  empty page (same bug class as MUFG/UBS BrassRing): offset 40 replayed the
  identical first job ID as offset 0 for keyword "engineer" (total=25, one
  page of 20 + a second of 5, then wraps). The `total` field also resets to
  0 on any offset > 0 even though `jobPostings` still contains real results
  (the Accenture/Northern Trust/MUFG bug) -- never used as a signal here.
  Fixed the same way as MUFG: memoize each keyword's first-page first job ID
  and treat its reappearance on a later page as "no more results" so
  matcher.py's `if not page: break` loop actually terminates.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://nasdaq.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/nasdaq/Global_External_Site/jobs"
_JOB_BASE = f"{_BASE_URL}/Global_External_Site"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/nasdaq/Global_External_Site"

# Tenant HTTP 400s on any limit above 20 (verified live: 20 -> 200, 25/30/40/
# 50/100 -> 400).
_PAGE_SIZE = 20
_MAX_LIMIT = 20

# India Location_Country WID -- the standard cross-tenant Workday India GUID
# (same as Fidelity/Wells Fargo/Citi/Northern Trust/MUFG/Accenture). Exposed
# under the capitalised "Location_Country" facet key on this tenant
# (discovered from an unfiltered search's own facets list: count=33).
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Pagination wrap-around tracking, keyed by keyword -- see module docstring.
_page1_first_id: dict[str, str] = {}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/Global_External_Site",
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


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    body = {
        "appliedFacets": {"Location_Country": [_INDIA_WID]},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

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
                raise RateLimitError("Nasdaq Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Nasdaq fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    # Detect the pagination wrap-around bug (see module docstring) before
    # doing any other work on this page.
    if postings:
        first_bullets = postings[0].get("bulletFields", [])
        first_id = first_bullets[0].strip() if first_bullets else ""
        if start == 0:
            if first_id:
                _page1_first_id[keyword] = first_id
        elif first_id and _page1_first_id.get(keyword) == first_id:
            return []

    jobs: list[dict] = []
    for p in postings:
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the Location_Country facet --
        # make sure the literal substring is present for matcher.py's
        # is_india_job() check. Multi-site rollups show "2 Locations" /
        # "3 Locations" with no country text at all.
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

        external_path = p.get("externalPath", "")
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

    Returns (description_text, posting_date).
    The startDate field in the detail response is already ISO format
    (YYYY-MM-DD), so no relative-date conversion is needed here.
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/Global_External_Site/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(2):
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
                raise RateLimitError("Nasdaq description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Nasdaq description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
