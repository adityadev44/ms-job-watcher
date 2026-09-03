"""Fetches Pfizer job listings via the Workday public REST API.

Pfizer's ATS is Workday, hosted at pfizer.wd1.myworkdayjobs.com
(tenant "pfizer", site "PfizerCareers" -- mixed case in the URL path,
unlike Target's lowercase "targetcareers"). Confirmed live 2026-09-03:
POST to /wday/cxs/pfizer/PfizerCareers/jobs returns real jobPostings,
and keyword search (searchText) genuinely narrows results server-side
(e.g. "python" and "software engineer" return different pools) --
unlike many Workday tenants in this repo, keywords are NOT ignored here.

Uses the capitalised "Location_Country" facet key (like MMC/State
Street/Target/eBay/GM), and the India WID is the same cross-tenant GUID
reused across Fidelity/Wells Fargo/Citi/Northern Trust/MUFG/Target --
this facet is reliable, not one of the broken/leaky ones (Micron/Verizon).

Pfizer's global India job pool on this tenant is small and volatile
(~17-22 jobs at any moment) and skews toward manufacturing/QA (Vizag
plant) and medical/commercial roles, not software engineering. Two
real AI/Python engineering reqs were confirmed to have existed recently
("AI Backend Python Engineer (Software Engineer II)" in Chennai,
"DS & AI Full Stack Engineer") but both are closed as of this writing --
the detail endpoint now 403s ("permission denied") for their IDs, and
they no longer appear in the live search results. No Hyderabad-based
posting exists in the current pool despite Pfizer's India GCC being
centered there; this is a real current snapshot, not a fetcher defect
(confirmed via both the India-faceted search and a raw "Hyderabad"
keyword search, both returning the same result set).

locationsText is usually "India - <City>" already containing "India"
(space-hyphen-space, not Target's comma style) but multi-site rollup
entries ("2 Locations", "3 Locations") omit it -- ", India" appended
when missing, safe because results are already pre-filtered by the
India country facet.

Two more Workday quirks confirmed live on this tenant, both guarded here:
- Page size is capped at 20 -- requesting limit=30 returns a raw HTTP 400
  (same class as Northern Trust's cap; harmless in practice since matcher.py
  always requests num=20, but clamped defensively via _MAX_LIMIT anyway).
- Pagination wraps around (same bug class as UBS BrassRing/MUFG): offset >=
  a keyword's real total does NOT return an empty jobPostings array, it
  silently replays the same page forever. Without the _page1_first_id guard
  below, every keyword with at least one hit would loop until max_listings
  is exhausted instead of stopping -- this was caught live during Step 8
  testing (a first `run_company.py pfizer` run ran for 25+ minutes before
  being killed, once traced back to this wraparound multiplying requests
  across most of the 10 default keywords).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://pfizer.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/pfizer/PfizerCareers/jobs"
_JOB_BASE = f"{_BASE_URL}/PfizerCareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/pfizer/PfizerCareers"

_PAGE_SIZE = 20

# India country WID -- the standard cross-tenant Workday "India" reference ID,
# exposed on this tenant under the "Location_Country" facet key.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# This tenant also caps page size at 20 -- requesting limit=30 returns a raw
# HTTP 400 (same class of cap as Northern Trust). matcher.py always calls
# with num=20 so this is defensive, not currently load-bearing.
_MAX_LIMIT = 20

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS BrassRing / MUFG): requesting offset >= total does NOT return
# an empty jobPostings array -- it silently re-returns the same page verbatim
# forever, with `total` reverting to 0. Verified live 2026-09-03 for "AI
# engineer" (total=11): offset=11 replayed the exact same 11 job IDs as
# offset=0. Without a guard, matcher.py's `if not page: break` pagination
# loop never terminates naturally for any keyword with a non-empty result --
# it keeps re-fetching the same page until max_listings is exhausted, for
# every keyword that returns at least one hit. Track each keyword's
# first-page first job ID and treat a repeat of it on a later page as "no
# more results".
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
    "Referer": f"{_BASE_URL}/PfizerCareers",
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
                raise RateLimitError("Pfizer Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Pfizer fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    # Detect the pagination wrap-around bug (see _page1_first_id comment
    # above) before doing any other work on this page.
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
        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the country facet -- make sure the
        # literal substring is present for matcher.py's india check (multi-site
        # rollups like "2 Locations"/"3 Locations" omit the country name).
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

        title = p.get("title", "").strip()
        if not title:
            continue

        external_path = p.get("externalPath", "")

        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets and bullets[0].strip() else external_path
        if not job_id:
            continue

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
    The startDate field in the detail response is already ISO (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/PfizerCareers/", 1)[-1]
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
                raise RateLimitError("Pfizer description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Pfizer description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
