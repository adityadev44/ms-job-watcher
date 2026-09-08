"""Fetches Quantiphi job listings via the Workday public REST API.

Quantiphi's ATS is Workday, hosted at quantiphi.wd1.myworkdayjobs.com
(tenant "quantiphi", site "Careers_at_Quantiphi") — confirmed live by
following the "Apply Now" link on quantiphi.com/careers/, which points
straight at that tenant (the informally-guessed "careers.quantiphi.com"
subdomain does not resolve — DNS NXDOMAIN as of 2026-09-08 — same class of
dead-branded-subdomain miss as CitiusTech/LTIMindtree).

No country-level location facet exists on this tenant — only a flat
"locations" facet nested under `locationMainGroup` listing individual
city/remote-region names with no country context. India cities are passed
explicitly under `appliedFacets.locations` (Barclays/Fractal pattern).
Trivandrum is a real, active Quantiphi office on this tenant and must still
be excluded downstream via config's `exclude_locations` (it's in Kerala).

`locationsText` in search results is a bare city/region name with no
country suffix ("IN MH Mumbai Eureka", "2 Locations" for multi-site
postings) — every returned job gets ", India" appended (safe because the
facet already pre-filters to India-only city WIDs).

Job IDs come from bulletFields (format "JRxxxxx"); the detail API's own
`country` field independently confirms India via the shared cross-tenant
WID `c4f78be1a8f14da0ab49ce1162348a5e` seen at Fidelity/Wells Fargo/Citi/
Northern Trust/MUFG.
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://quantiphi.wd1.myworkdayjobs.com"
_TENANT_PATH = "Careers_at_Quantiphi"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/quantiphi/{_TENANT_PATH}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_TENANT_PATH}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/quantiphi/{_TENANT_PATH}"
_PAGE_SIZE = 20

# India office location WIDs for Quantiphi's Workday tenant, discovered via
# the "locationMainGroup" -> "Locations" nested facet in a live unfiltered
# search on 2026-09-08. Includes Trivandrum (Kerala); exclude_locations in
# config handles it downstream.
_INDIA_WIDS = [
    "004c6095ba3b100115771c2cfdde0000",  # IN KA Bengaluru
    "004c6095ba3b100115771a5d467d0000",  # IN KL Trivandrum
    "004c6095ba3b100115771fcae6340000",  # IN MH Mumbai Eureka
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
    "Referer": f"{_BASE_URL}/{_TENANT_PATH}",
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
        "appliedFacets": {"locations": _INDIA_WIDS},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(_SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Quantiphi Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Quantiphi fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Quantiphi fetch: no response — {last_exc}")

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = ""
        for field in p.get("bulletFields", []):
            field = field.strip()
            if re.match(r"^JR\d+$", field, re.IGNORECASE):
                job_id = field.upper()
                break
        if not job_id:
            m = re.search(r"_(JR\d+)$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Bare city/region string / "N Locations" — no country. Facet
        # already restricts to India-only city WIDs, so appending is safe.
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
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_TENANT_PATH}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("Quantiphi description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Quantiphi description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Quantiphi description fetch: no response — {last_exc}")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
