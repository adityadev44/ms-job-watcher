"""Fetches Mastercard job listings via the Workday public REST API.

Mastercard's ATS is Workday, hosted at mastercard.wd1.myworkdayjobs.com
(tenant "mastercard", site "CorporateCareers").

Unlike Wells Fargo / Fidelity / Marsh McLennan, this tenant exposes no
country-level location facet (no "locationCountry" / "Location_Country" key
works — both return HTTP 400). The only location facet reported in the
facets response is "locationMainGroup", a flat list of ~41 individual
city/site WIDs with no country grouping — but submitting that key in
appliedFacets also returns HTTP 400.

Verified live on 2026-06-25: the correct appliedFacets key for filtering is
"locations" (same key Maersk's tenant uses), not "locationMainGroup". All 8
India city WIDs (Gurgaon x2, Hyderabad, Mumbai, Navi Mumbai/Finicity, Pune x2,
Vadodara) are passed together — confirmed 227 India results for an empty
search and 111 for "software engineer" with this facet applied.

Same as Maersk: some postings report locationsText as "2 Locations" when a
role is open at multiple sites. Since we only ever filter by India WIDs,
normalise that to "India" so matcher.py's is_india_job() doesn't skip it.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://mastercard.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/mastercard/CorporateCareers/jobs"
_JOB_BASE = f"{_BASE_URL}/CorporateCareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/mastercard/CorporateCareers"
_PAGE_SIZE = 20

# India location WIDs for Mastercard's Workday tenant, discovered via the
# "locationMainGroup" facet in a live, unfiltered search (2026-06-25).
# There is no country-level facet on this tenant, so all known India city
# WIDs are passed together under the "locations" appliedFacets key.
_INDIA_WIDS = [
    "8eab563831bf10acb97b7fba5feff76e",  # Gurgaon, India
    "2e83df2db0851073b1d3be60012912d5",  # Gurgaon, India (alt)
    "8eab563831bf10acb9d6f1103d78fa67",  # Hyderabad, India
    "8eab563831bf10acbb7b5bf86d570af1",  # Mumbai, India
    "28905a74db1b10019f5bb16c36030000",  # Navi Mumbai, India (Finicity)
    "8eab563831bf10acbc722e4859721571",  # Pune, India
    "8ffae25149e210718ae5b2e1cb1993bb",  # Pune, India (alt)
    "85a5bdf4e1831035984400a2fb698c94",  # Vadodara, India
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
    "Referer": f"{_BASE_URL}/CorporateCareers",
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
    body = {
        "appliedFacets": {"locations": _INDIA_WIDS},
        "limit": num,
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
                raise RateLimitError("Mastercard Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Mastercard fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(R-\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(R-\d+)(-\d+)?$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the WID facet — same fix as
        # Maersk: Workday shows "2 Locations" when a role is open at
        # multiple sites, which would otherwise fail the India text check.
        if "india" not in loc.lower():
            loc = "India"

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
        ext_path = "/" + application_url.split("/CorporateCareers/", 1)[-1]
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
                raise RateLimitError("Mastercard description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Mastercard description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
