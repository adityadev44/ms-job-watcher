"""Fetches Accenture job listings via the Workday public REST API.

Accenture's ATS is Workday, hosted at accenture.wd103.myworkdayjobs.com.
The Workday CXS endpoint accepts plain POST requests — no browser needed.

India is filtered server-side using the locationCountry facet WID discovered
via probe on 2026-06-26. India total: ~24,731 jobs. The API returns at most
2000 results per query; require_tech_in_description in the runner handles precision.

Locations in the search response are city-only ("Bengaluru", "Hyderabad").
This fetcher appends ", India" so the shared is_india_job() check passes.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://accenture.wd103.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/accenture/AccentureCareers/jobs"
_JOB_BASE = f"{_BASE_URL}/AccentureCareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/accenture/AccentureCareers"

_PAGE_SIZE = 20

# India locationCountry WID for Accenture's Workday tenant.
# Verified 2026-06-26: ~24,731 India jobs visible via this facet.
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
    "Referer": f"{_BASE_URL}/AccentureCareers",
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

    # "posted 30+ days ago" → treat as 30 days
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
                raise RateLimitError("Accenture Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Accenture fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        # Job ID from bulletFields[0] — format: ATCI-XXXXXXX-SXXXXXXX
        bullet = p.get("bulletFields", [])
        job_id = bullet[0].strip() if bullet else ""
        if not job_id:
            # Fallback: extract from externalPath end segment
            m = re.search(r"_(ATCI-[\w-]+|R\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        # City is in bulletFields[1]; append ", India" so is_india_job() passes
        city = bullet[1].strip() if len(bullet) > 1 else ""
        loc = f"{city}, India" if city else "India"

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
    # Transform application URL to JSON API URL:
    # https://accenture.wd103.myworkdayjobs.com/AccentureCareers/job/...
    # → https://accenture.wd103.myworkdayjobs.com/wday/cxs/accenture/AccentureCareers/job/...
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/AccentureCareers/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

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
                raise RateLimitError("Accenture description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Accenture description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    # startDate is already YYYY-MM-DD from the API
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
