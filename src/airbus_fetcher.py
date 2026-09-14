"""Fetches Airbus India job listings via the Workday public REST API.

ATS identification (Step 1, verified live 2026-09-13): careers.airbus.com's
"Search Jobs" link resolves to a first-party Workday tenant,
`ag.wd3.myworkdayjobs.com/Airbus` -- confirmed via `POST /wday/cxs/ag/Airbus/
jobs` returning real jobPostings. No frontend skin involved here (unlike
Boeing/GE Aerospace/GE HealthCare in this repo) -- Airbus's own careers site
links directly to the Workday-hosted board.

Like GE Aerospace, this tenant has no top-level `locationCountry`/
`Country_and_Jurisdiction` facet key -- the country facet is nested one
level deep under an outer `locationMainGroup` facet (siblings: `locations`,
a flat ~183-entry city list). Found by inspecting the unfiltered facets
response. Despite being nested in the *response*, the facet is applied the
normal flat way in the request body (`appliedFacets.locationCountry`), same
as every other Workday tenant's top-level facet -- confirmed working live.
India's country WID is `c4f78be1a8f14da0ab49ce1162348a5e`, which happens to
be the exact same GUID this repo already uses for Citi's India facet (see
`citi_fetcher.py`/PLAYBOOK.md "Key Bugs") -- Workday appears to reuse a
standardized country WID across at least some tenants; treat that overlap
as a coincidence of the platform, not something to hardcode as a
cross-tenant assumption elsewhere.

Verified live 2026-09-13: unfiltered global pool is ~2000 jobs; the India
facet alone returns 224 jobs, almost entirely "Bangalore Area" with a few
Gurugram/New Delhi/Hyderabad postings -- a real, current, sizeable India
presence (Airbus's Bengaluru engineering/R&D hub), including genuine
software/AI titles seen directly in a live sample: "AI Engineer",
"FullStack Developer (React,AWS & Python)", "Software Engineer - PCP
Platform(AWS Cloud) - IaC", "Platform Engineer - Open Shift Platform",
"Lead Software Engineer", "Lead Developer - SRE (Digital Archiving)",
"Cyber Security Architect", "Solution Architect". `searchText` genuinely
narrows server-side (224 -> 105 for "software") -- do NOT add this slug to
_IGNORES_KEYWORDS.

Descriptions are not inline in the search response -- `fetch_job_description`
uses the standard Workday CXS job-detail endpoint (same shape as every other
Workday fetcher in this repo). `startDate` is already ISO (YYYY-MM-DD).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://ag.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/ag/Airbus/jobs"
_JOB_BASE = f"{_BASE_URL}/Airbus"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/ag/Airbus"

_PAGE_SIZE = 20
_MAX_LIMIT = 20  # defensive cap, consistent with every other Workday fetcher here

# India country WID, found via the unfiltered facets response (nested under
# locationMainGroup -> locationCountry). See module docstring for the
# coincidental overlap with Citi's WID.
_INDIA_COUNTRY_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/Airbus",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


def _parse_posted_on(posted_on: str) -> str:
    """Convert Workday's relative 'Posted N Days Ago' search-result string
    to 'YYYY-MM-DD'. Same helper as geaerospace_fetcher.py — used here as a
    best-effort list-level date so sorting/logging works even if a later
    per-job detail fetch fails; the detail page's ISO startDate still wins
    whenever it's available (see matcher.py)."""
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
        "appliedFacets": {"locationCountry": [_INDIA_COUNTRY_WID]},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
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
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Airbus Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Airbus fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    jobs: list[dict] = []
    for p in postings:
        loc = p.get("locationsText", "").strip()
        # Pre-filtered to India via the country facet, but locationsText for
        # this tenant is city-only ("Bangalore Area", "Gurugram") -- append
        # ", India" so matcher.py's is_india_job() substring check passes,
        # same pattern as GE Aerospace/Invesco.
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

    Returns (description_text, posting_date). `startDate` in the detail
    response is already ISO (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/Airbus/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("Airbus description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Airbus description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
