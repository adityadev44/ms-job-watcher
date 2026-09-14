"""Fetches GE Vernova job listings via the Workday public REST API.

GE Vernova is the energy-focused entity spun off from the original General
Electric conglomerate in 2024 (distinct from GE Aerospace and GE HealthCare,
both of which already have their own fetchers in this repo). Its careers
site (careers.gevernova.com) links out to a real Workday tenant:
`gevernova.wd5.myworkdayjobs.com`, site "Vernova_ExternalSite". Confirmed
live 2026-09-14: `POST /wday/cxs/gevernova/Vernova_ExternalSite/jobs`
returns real jobPostings (total ~2107 globally).

Like GE Aerospace, this tenant has NO country-level facet -- the only
location facet Workday exposes is a flat city list (`locationMainGroup` ->
`locations`, 424 entries, no country grouping). Found India by scanning
that facet's own descriptors for known India city names (Bengaluru,
Chennai, Gurugram, Hyderabad, "Hyderabad TS IN 26", Mumbai, Noida, Pune,
Vadodara) rather than assuming any "India" substring exists anywhere (same
false-positive risk PLAYBOOK.md documents for PayPal/FactSet/Micron). Each
city can appear as several *different* facet IDs (distinct office/site
records) -- confirmed via facet response: 22 distinct India-city WIDs
total. All 22 are hardcoded and applied via `appliedFacets.locations`,
same pattern as GE Aerospace's/Barclays' city-WID lists. Total India pool
at investigation time: 303 jobs.

Because filtering happens via specific city WIDs (not a generic country
facet), any job matching this facet is already confirmed India-based --
including postings whose `locationsText` shows an ambiguous "N Locations"
label (Workday's standard multi-site-req collapsing). No per-posting detail
lookup is needed to resolve those (unlike SimCorp, whose ambiguous postings
were NOT pre-filtered by any location facet at all) -- they are simply
relabeled as "India", same shortcut used by the Maersk/GE Aerospace/Rockwell
fetchers in this repo.

Pune is included in the WID list deliberately (matches this repo's
established policy of keeping fetcher-level India scope complete and
letting `exclude_locations` in config.yaml own the exclusion decision).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://gevernova.wd5.myworkdayjobs.com"
_TENANT_PATH = "/wday/cxs/gevernova/Vernova_ExternalSite"
_SEARCH_URL = f"{_BASE_URL}{_TENANT_PATH}/jobs"
_JOB_BASE = f"{_BASE_URL}/Vernova_ExternalSite"
_DETAIL_BASE = f"{_BASE_URL}{_TENANT_PATH}"

_PAGE_SIZE = 20
_MAX_LIMIT = 20  # confirmed: >20 returns a clean HTTP 400 (Northern Trust-style cap)

# India city location WIDs -- tenant-specific, found by scanning the
# unfiltered `locationMainGroup` -> `locations` facet for known India city
# names (no country-level facet exists on this tenant). Each city can carry
# multiple distinct facet IDs (separate office/site records).
_INDIA_LOCATION_WIDS = [
    "864337d87169100160cd537047ea0000",  # Bengaluru
    "4bbb63dfb11f10016153612c2ede0000",  # Bengaluru
    "4bbb63dfb11f10016152765fd1960000",  # Chennai
    "4bbb63dfb11f1001616b542440cc0000",  # Chennai
    "864337d87169100160ddfa3b6eff0000",  # Chennai
    "864337d87169100160e24c3ebcc30000",  # Gurugram
    "864337d87169100160dadf1cc2120000",  # Hyderabad
    "4bbb63dfb11f10016151c63837d90000",  # Hyderabad
    "4bbb63dfb11f1001614e18ef70a20000",  # Hyderabad
    "4bbb63dfb11f1001616a2d9858230000",  # Hyderabad
    "4bbb63dfb11f1001615790eb8ad60000",  # Hyderabad
    "4bbb63dfb11f100161500a4f56630000",  # Hyderabad
    "e22d931a9cea10010899f780cf980000",  # Hyderabad TS IN 26
    "4bbb63dfb11f1001616da84b60d80000",  # Mumbai
    "4bbb63dfb11f10016169ecabe6350000",  # Noida
    "4bbb63dfb11f100161668b0884700000",  # Noida
    "4bbb63dfb11f10016161ed48ee6c0000",  # Noida
    "864337d87169100160de166a48f70000",  # Noida
    "4bbb63dfb11f100161515fcaec290000",  # Pune
    "864337d87169100160dcc66daf360000",  # Pune
    "4bbb63dfb11f1001615d6f9864c90000",  # Vadodara
    "4bbb63dfb11f1001615287d298720000",  # Vadodara
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
    "Referer": f"{_BASE_URL}/Vernova_ExternalSite",
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
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
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
                raise RateLimitError("GE Vernova Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"GE Vernova fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the city-WID facet -- make sure
        # the literal substring is present for matcher.py's india check
        # (multi-site rollups like "2 Locations" omit the country entirely).
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
        ext_path = "/" + application_url.split("/Vernova_ExternalSite/", 1)[-1]
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
                raise RateLimitError("GE Vernova description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"GE Vernova description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
