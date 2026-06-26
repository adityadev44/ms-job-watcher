"""Fetches Citi (Citigroup) job listings via the Workday public REST API.

Citi's ATS is Workday, hosted at citi.wd5.myworkdayjobs.com (tenant "citi",
site "2"). Confirmed live 2026-06-26:
POST to /wday/cxs/citi/2/jobs returns real jobPostings.

India is filtered server-side via the Country_and_Jurisdiction facet
(WID c4f78be1a8f14da0ab49ce1162348a5e — the same global "India" country
reference ID used across other Workday tenants). Note: the facet key here is
"Country_and_Jurisdiction", not "locationCountry" as in Fidelity/Wells Fargo.

Some India postings report locationsText without "India" in the text:
  - "2 Locations" — multi-site rollup
  - "DLF CYBERCITY 12B" — Gurugram office (DLF CyberCity is in Haryana, India)
Since the Country_and_Jurisdiction facet already guarantees every result is
India, fetch_jobs appends ", India" when "india" is absent — same fix as
Fidelity, Maersk, Marsh McLennan, Synchrony.

Citi has 1 245 India jobs total, ~339 when keyword = "software engineer".
Pagination works normally with limit=20; no page-wrap quirk observed.
The detail endpoint's startDate is already ISO format (YYYY-MM-DD), so no
relative-date conversion is needed for descriptions.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://citi.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/citi/2/jobs"
_JOB_BASE = f"{_BASE_URL}/2"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/citi/2"

_PAGE_SIZE = 20

# India country WID -- the standard Workday global India reference ID.
# Exposed under the "Country_and_Jurisdiction" facet key on this tenant
# (different from the "locationCountry" key used by Fidelity and Wells Fargo,
# but the WID itself is identical).
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/2",
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
        "appliedFacets": {"Country_and_Jurisdiction": [_INDIA_WID]},
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
                raise RateLimitError("Citi Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Citi fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the Country_and_Jurisdiction facet --
        # make sure the literal substring is present for matcher.py's india check.
        # (Multi-site rollups report "2 Locations"; the Gurugram DLF office shows
        # as "DLF CYBERCITY 12B" with no country text.)
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
    The startDate field in the detail response is already ISO format (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/2/", 1)[-1]
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
                raise RateLimitError("Citi description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Citi description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
