"""Fetches Walmart Global Tech India job listings via the Workday public
REST API.

Walmart's careers site (careers.walmart.com) is a Radancy/Phenom-style
frontend skin, but the actual apply flow and search backend sit on Workday
at walmart.wd504.myworkdayjobs.com (tenant "walmart", site
"WalmartExternal"). Confirmed live 2026-09-03 via direct DevTools-style probing:
POST to /wday/cxs/walmart/WalmartExternal/jobs returns real jobPostings.
NOTE: walmart.wd5.myworkdayjobs.com (a different, older host referenced by
some external job aggregators) returns HTTP 422 for this tenant/site
combo -- wd504 is the live one. Prior third-party-aggregator research
calling this a "custom" ATS was wrong; it is standard Workday CXS, same
shape as Target/Citi/MUFG/etc.

This is Walmart's single global Workday tenant covering ALL Walmart/Sam's
Club hiring worldwide (retail stores, supply chain, healthcare, corporate,
and tech) -- there is no separate "Global Tech India" sub-tenant. The
"Technology" jobFamilyGroup facet (id e83ebdbd2a0a01e7e1477a8948e904c6) has
973 open reqs globally, ~178 in India, but that facet is NOT applied here --
generic retail-store titles ("Stock Unloader Associate", "GM Coach") never
match the shared title_family list downstream, so scoping to the tech
facet server-side is unnecessary; the existing filter pipeline already
excludes them cheaply before any detail fetch. This mirrors the Genpact/
Fiserv pattern of relying on client-side filtering rather than a narrow
server-side facet.

India is filtered server-side via the standard lowercase "locationCountry"
facet key (like Fidelity/Wells Fargo/Northern Trust, NOT the capitalised
"Location_Country" of Target/MMC/State Street, NOT Citi's
"Country_and_Jurisdiction"). India WID c4f78be1a8f14da0ab49ce1162348a5e is
the same cross-tenant GUID reused across Fidelity/Wells Fargo/Citi/Northern
Trust/Accenture/MUFG. ~277 total India jobs across all departments as of
2026-09-03 (Karnataka/Bengaluru ~221, Tamil Nadu/Chennai ~61 -- matches the
brief's "Bengaluru primary, Chennai secondary" description; Chennai is
covered by config's default `exclude_locations`).

Same bug class as MUFG/Accenture/Northern Trust: this tenant HTTP 400s on
any `limit` above 20 (tested live: 20 -> 200 OK, 21/25/30/50/100 -> 400).
fetch_jobs clamps `num` to 20 defensively.

Same pagination wraparound bug as MUFG/UBS BrassRing: requesting
offset >= total does NOT return an empty jobPostings array -- it silently
re-returns page 1 verbatim forever (verified live for "AI engineer",
total=220: offset 300 replayed the exact same 20 job IDs as offset 0).
Track each keyword's first-page first job ID and treat a repeat of it on a
later page as "no more results" so matcher.py's `if not page: break`
pagination loop actually terminates.

locationsText is almost always a facility name with no "India" substring
at all (e.g. "IN KA BANGALORE Home Office PW II", "IN TN CHENNAI Home
Office Capita Land") -- the leading "IN"/"TN"/"KA" are state/country
abbreviation codes, not the word "India". Since the country facet already
guarantees every result is genuinely India, ", India" is appended
client-side when the literal substring is absent (same fix as
Citi/Barclays/Maersk/MUFG); multi-site rollup postings showing "N
Locations" get "India" as their whole location string for the same reason.
Note the raw text still contains the literal city name ("CHENNAI"), so
config's exclude_locations substring check for Chennai/Tamil Nadu still
works correctly without the append.

Keyword search is genuinely server-side (different totals per keyword
verified live: "software engineer" 233, ".NET developer" 52, "angular" 7,
"AI engineer" 219, "C# developer" 18) -- NOT a no-op like several other
Workday tenants in this repo, so walmart is NOT added to
_IGNORES_KEYWORDS.

Descriptions are Java/Node/Python-stack heavy in the sampled postings
(Spring Boot, Kafka, Kubernetes, Azure/GCP, React/Angular, microservices) --
no inline description in the search response, so a detail fetch via the
Workday CXS JSON API is required per job (same pattern as
Target/MUFG/Wells Fargo); `startDate` in the detail response is already
ISO (YYYY-MM-DD).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://walmart.wd504.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/walmart/WalmartExternal/jobs"
_JOB_BASE = f"{_BASE_URL}/WalmartExternal"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/walmart/WalmartExternal"

# Tenant HTTP 400s on any limit above 20 (verified live: 20 -> 200, 21 -> 400).
_PAGE_SIZE = 20
_MAX_LIMIT = 20

# India country WID -- the standard cross-tenant Workday India reference ID,
# exposed on this tenant under the lowercase "locationCountry" facet key
# (discovered from an unfiltered search's own facets list).
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Pagination wraps around past the real result count for a keyword (same bug
# class as MUFG/UBS BrassRing): requesting offset >= total does NOT return
# an empty jobPostings array -- it silently re-returns page 1 verbatim,
# forever. Verified live for "AI engineer" (total=220): offset 300 replayed
# the exact same 20 job IDs as offset 0. Track each keyword's first-page
# first job ID and treat a repeat of it on a later page as "no more
# results" so matcher.py's `if not page: break` pagination loop actually
# terminates instead of re-fetching the same page until max_listings is
# exhausted.
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
    "Referer": f"{_BASE_URL}/WalmartExternal",
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
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
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
                raise RateLimitError("Walmart Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Walmart fetch failed: {exc}") from exc

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
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the locationCountry facet --
        # make sure the literal substring is present for matcher.py's
        # is_india_job() check. Facility names ("IN KA BANGALORE Home
        # Office PW II") carry no "India" text at all; multi-site rollups
        # show "N Locations".
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
        ext_path = "/" + application_url.split("/WalmartExternal/", 1)[-1]
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
                raise RateLimitError("Walmart description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Walmart description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
