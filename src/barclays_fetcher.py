"""Fetches Barclays job listings via the Workday public REST API.

Barclays' ATS is Workday, hosted at barclays.wd3.myworkdayjobs.com
(tenant "barclays", site "External_Career_Site_Barclays").

No country-level location facet (locationCountry) is honoured by this
tenant — requests with that key return all 1017 jobs unfiltered. The
correct approach (same pattern as Mastercard / Maersk / Synchrony) is
to pass all known India city/site WIDs under the "locations" key of
appliedFacets. There are 11 India office locations totalling ~598 jobs
as of 2026-06-26.

locationsText in search results contains the building name but not the
country (e.g. "Gurugram, DLF Downtown" — no "India" suffix). Every
returned job gets ", India" appended so matcher.py's is_india_job()
works. The city prefix (Pune, Chennai) is preserved so exclude_locations
can still filter those cities out correctly.

Job IDs come from bulletFields as "JR-XXXXXXX" strings. The externalPath
sometimes carries a deduplication suffix (e.g. _JR-0000109599-1 for a
re-posted requisition) — the canonical ID from bulletFields is preferred.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://barclays.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/barclays/External_Career_Site_Barclays/jobs"
_JOB_BASE = f"{_BASE_URL}/External_Career_Site_Barclays"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/barclays/External_Career_Site_Barclays"
_PAGE_SIZE = 20

# India office location WIDs for Barclays' Workday tenant.
# Discovered via the "locationMainGroup" -> "Locations" nested facet in a
# live unfiltered search on 2026-06-26. Passed under "locations" in
# appliedFacets — same key used by Mastercard and Maersk.
# Includes Chennai and Pune; exclude_locations in config handles those.
_INDIA_WIDS = [
    "112c0542820110016380b1b7e7f40000",  # Barclays Bank Plc - GIFT City Branch (1)
    "1110a9ca6540100196e1f0c315e90000",  # Bengaluru, Maruthi Onyx - TESCO TSA (38)
    "112c0542820110016378a0a3e68d0000",  # Ceejay House, Mumbai (2)
    "112c05428201100163788a5627320000",  # Chennai, DLF IT Park (49)
    "253dfca5ccfc10016b1361f46cfe0000",  # Gurugram, DLF Downtown (17)
    "1ab48a98eb7c1001634cf23b210c0000",  # Mumbai, Altimus (4)
    "b8f75d1cb9781000cd91027a85e30000",  # Mumbai, Nirlon Knowledge Park - BX (22)
    "1ab48a98eb7c100163423aff71b80000",  # Mumbai, Nirlon Knowledge Park - IB (13)
    "112c0542820110016377af553b050000",  # New Delhi, Eros Corporate Tower (2)
    "112c0542820110016377c02c6ef00000",  # Noida, Candor TechSpace (31)
    "112c05428201100163763bd6ad400000",  # Pune, Gera Commerzone SEZ (449)
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
    "Referer": f"{_BASE_URL}/External_Career_Site_Barclays",
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
                raise RateLimitError("Barclays Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Barclays fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        # Job ID from bulletFields — Barclays uses "JR-XXXXXXX" format
        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(JR-\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            # Fallback: extract from externalPath (e.g. _JR-0000109599-1)
            m = re.search(r"_(JR-\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # locationsText omits the country (e.g. "Gurugram, DLF Downtown").
        # Append ", India" so is_india_job() passes and city-level excludes
        # still work ("Pune, Gera Commerzone SEZ, India" matches "pune").
        if "india" not in loc.lower():
            loc = (loc + ", India") if loc else "India"

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
    # Transform application URL to JSON API URL:
    # https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays/job/...
    # -> https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays/job/...
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/External_Career_Site_Barclays/", 1)[-1]
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
                raise RateLimitError("Barclays description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Barclays description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    # startDate is already YYYY-MM-DD from the API
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
