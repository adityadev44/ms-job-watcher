"""Fetches Synchrony Financial job listings via the Workday public REST API.

Synchrony's ATS is Workday, hosted at synchronyfinancial.wd5.myworkdayjobs.com
(tenant "synchronyfinancial", site "careers"). Confirmed live 2026-06-25:
POST to /wday/cxs/synchronyfinancial/careers/jobs returns real jobPostings.

This tenant exposes NO country-level location facet (only individual city /
region WIDs nested under "locationMainGroup" → "locations", same situation as
Fiserv). India-related entries discovered from the live facets response:

    Hyderabad IN                 8bc859bd8bb6106afb88eb9626f71d0f
    Remote Central Region IN     a6ed3d0b32821000d0c8e5d78e270000
    Remote Eastern Region IN     a6ed3d0b32821000d0917c512e370000
    Remote Northern Region IN    e63263cf28701000d043ef91b6630000
    Remote Southern Region IN    fe8934a956f01000d09bd3a707610000
    Remote Western Region IN     fe8934a956f01000d08acbb863480000

These are passed as the "locations" facet key (NOT "locationMainGroup" —
that key name returns HTTP 400 on this tenant; verified live).

Two tenant-specific quirks discovered via live probing:

1. limit > 20 returns HTTP 400 ("Bad Request"). This tenant hard-caps page
   size at 20 — unlike Wells Fargo/Fiserv/Fidelity where 20 is just the
   chosen default. _PAGE_SIZE is enforced as a ceiling here.
2. Requesting an offset at or beyond the API's reported "total" does NOT
   return an empty page — it silently wraps around and re-returns page 1.
   Confirmed live: with total=28, offset=40/60/80... all returned the same
   20 jobs as offset=0. fetch_jobs() therefore caps `num` so start+num never
   exceeds the freshly-fetched `total`, and returns [] once start >= total,
   so matcher.py's "stop when page is empty" pagination loop terminates
   correctly instead of looping on duplicate pages.

Many India postings report locationsText as a multi-site rollup like
"6 Locations" or "5 Locations" instead of a literal "India" substring, even
though the search was already restricted to the India-only WIDs above (the
job detail JSON confirms country.descriptor == "India" for these). Same fix
as Fidelity/Maersk/Marsh McLennan: append ", India" when missing — safe
because the facet already guarantees every result is India.

The detail JSON's own country WID for India is
c4f78be1a8f14da0ab49ce1162348a5e — the same global "India" reference ID seen
across other Workday tenants (Wells Fargo, Fidelity, Marsh McLennan), even
though this tenant's *search* facet has to use city/region WIDs instead of a
country WID because no country-level facet is exposed in the search facets.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://synchronyfinancial.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/synchronyfinancial/careers/jobs"
_JOB_BASE = f"{_BASE_URL}/careers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/synchronyfinancial/careers"

# This tenant rejects limit > 20 with HTTP 400 — confirmed live 2026-06-25.
_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 20

# India location WIDs for Synchrony's Workday tenant (locations facet).
# No country-level facet exists on this tenant — verified via live facets
# response (only jobFamilyGroup / workerSubType / timeType / locationMainGroup
# facet groups are exposed; locationMainGroup itself returns HTTP 400 when
# passed directly as a facet key, so individual location WIDs are used).
_INDIA_LOCATION_WIDS = [
    "8bc859bd8bb6106afb88eb9626f71d0f",  # Hyderabad IN
    "a6ed3d0b32821000d0c8e5d78e270000",  # Remote Central Region IN
    "a6ed3d0b32821000d0917c512e370000",  # Remote Eastern Region IN
    "e63263cf28701000d043ef91b6630000",  # Remote Northern Region IN
    "fe8934a956f01000d09bd3a707610000",  # Remote Southern Region IN
    "fe8934a956f01000d08acbb863480000",  # Remote Western Region IN
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
    "Referer": f"{_BASE_URL}/careers",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper — Workday returns relative strings like "Posted 3 Days Ago"
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
    # This tenant hard-caps limit at 20 (HTTP 400 above that) — enforce it
    # regardless of what the caller passes.
    page_size = min(num, _MAX_PAGE_SIZE)

    body = {
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
        "limit": page_size,
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
                raise RateLimitError("Synchrony Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Synchrony fetch failed: {exc}") from exc

    data = r.json()
    total = data.get("total") or 0

    # Tenant quirk: requesting an offset >= total does NOT return an empty
    # page — it wraps around and re-returns page 1. Stop here so matcher.py's
    # pagination loop (which relies on an empty page to terminate) doesn't
    # spin forever re-processing the same jobs.
    if start >= total:
        return []

    jobs: list[dict] = []
    for p in data.get("jobPostings", []):
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the locations facet above — make
        # sure the literal substring is present for matcher.py's india check.
        # (Multi-site postings report e.g. "6 Locations" with no country text.)
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
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/careers/", 1)[-1]
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
                raise RateLimitError("Synchrony description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Synchrony description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
