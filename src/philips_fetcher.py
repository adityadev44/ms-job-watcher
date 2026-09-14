"""Fetches Philips job listings via the Workday public REST API.

The public-facing careers.philips.com frontend IS Phenom People (same CDN
family as Morningstar/GE Aerospace/GE HealthCare/Cisco -- `phApp.ddo` SSR
blob, refNum "PHILUS") -- but every job record embedded in that SSR blob
carries its own `applyUrl` pointing at `philips.wd3.myworkdayjobs.com`,
exposing the real backend ATS: Workday, tenant "philips", site
"jobs-and-careers" (confirmed live 2026-09-14 via
`POST /wday/cxs/philips/jobs-and-careers/jobs` returning real jobPostings
-- same "Phenom skin over a different real ATS" shape as GE Aerospace/GE
HealthCare in this repo). The Phenom frontend layer is never touched here.

Unlike GE Aerospace (which only has a flat city-level facet), this tenant
DOES expose a usable country facet -- but it's nested two levels deep like
Nvidia's tenant: the real key is `locationHierarchy1` (under the outer
`locationMainGroup` facet, sibling to `locationHierarchy2` = State/Province
and `locations` = City). India's WID (`6e1b2a934716103c2adde1d57e7700ea`)
is tenant-specific, not the shared cross-tenant GUID used by Adobe/Citi/
Rockwell. ~63 India jobs at investigation time, including a genuine C#
.NET Full Stack Developer role (verified live) and several "RD Software"
titles (Bangalore/Pune).

Like Rockwell/GE Vernova/Adobe, `limit` > 20 returns a clean HTTP 400
(Northern Trust-style cap) -- clamped defensively via `_MAX_LIMIT`.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://philips.wd3.myworkdayjobs.com"
_TENANT_PATH = "/wday/cxs/philips/jobs-and-careers"
_SEARCH_URL = f"{_BASE_URL}{_TENANT_PATH}/jobs"
_JOB_BASE = f"{_BASE_URL}/jobs-and-careers"
_DETAIL_BASE = f"{_BASE_URL}{_TENANT_PATH}"

_PAGE_SIZE = 20
_MAX_LIMIT = 20  # confirmed: >20 returns a clean HTTP 400 (Northern Trust-style cap)

# India country WID -- tenant-specific (Philips' own facet ID, not the
# shared cross-tenant GUID), exposed under the nested `locationHierarchy1`
# facet key (same "nested two levels deep" shape as Nvidia's tenant).
_INDIA_WID = "6e1b2a934716103c2adde1d57e7700ea"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/jobs-and-careers",
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
        "appliedFacets": {"locationHierarchy1": [_INDIA_WID]},
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
                raise RateLimitError("Philips Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Philips fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the country facet -- make sure the
        # literal substring is present for matcher.py's india check (multi-site
        # rollups like "2 Locations" omit the country name entirely).
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
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/jobs-and-careers/", 1)[-1]
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
                raise RateLimitError("Philips description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Philips description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
