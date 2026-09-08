"""Fetches Franklin Templeton job listings via the Workday public REST API.

Franklin Templeton's ATS is Workday, hosted at
franklintempleton.wd5.myworkdayjobs.com (tenant "franklintempleton", site
"Primary-External-1"). Confirmed live 2026-09-08 via direct POST to
/wday/cxs/franklintempleton/Primary-External-1/jobs -- real jobPostings come
back, and apply links resolve on this same tenant/site.

India is filtered server-side via the "locationCountry" facet (nested one
level under an outer "locationMainGroup" facet group in the raw facets
response, but -- like Fidelity/Wells Fargo/Citi -- the *inner* facetParameter
name "locationCountry" is itself the usable top-level appliedFacets key, no
prefix needed). India WID c4f78be1a8f14da0ab49ce1162348a5e is the same
shared cross-tenant India GUID seen at Fidelity/Wells Fargo/Citi/Northern
Trust/MUFG/Accenture/SS&C -- 67 India jobs confirmed live at time of writing,
real Hyderabad/Mumbai/New Delhi tech + fund-ops roles (Associate Software
Engineer, Data Engineer, Senior Software Engineer (Java & React), Sr AI/ML
Engineer - GenAI & Agentic AI, UI Architect, SDET, etc).

Keyword search (searchText) IS genuinely server-side (confirmed: empty
query returns 67, ".NET" narrows to 9).

Quirk (same bug class as Nvidia/MUFG/Shell): tenant HTTP 400s on any `limit`
above 20 (tested: 20 -> 200 OK, 50 -> 400). fetch_jobs clamps `num` to 20.
Offset pagination is genuine (offset=20 returns a different page than
offset=0 -- no wraparound observed).

Most India postings' locationsText already contains "India" (e.g.
"Hyderabad, Telangana, India"), but some site-code-only postings show a bare
facility code (e.g. "FTS-Hyderabad-F3354") with no "India" substring --
", India" is appended client-side when absent, same fix as Fidelity/Nvidia.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://franklintempleton.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/franklintempleton/Primary-External-1/jobs"
_JOB_BASE = f"{_BASE_URL}/Primary-External-1"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/franklintempleton/Primary-External-1"

# Tenant HTTP 400s on any limit above 20 (verified live: 20 -> 200, 50 -> 400).
_PAGE_SIZE = 20
_MAX_LIMIT = 20

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
    "Referer": f"{_BASE_URL}/Primary-External-1",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


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
                raise RateLimitError("Franklin Templeton Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Franklin Templeton fetch failed: {exc}") from exc

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
        # Already pre-filtered to India via the locationCountry facet -- make
        # sure the literal substring is present for matcher.py's india check.
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
        ext_path = "/" + application_url.split("/Primary-External-1/", 1)[-1]
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
                raise RateLimitError("Franklin Templeton description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Franklin Templeton description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
