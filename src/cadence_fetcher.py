"""Fetches Cadence Design Systems job listings via the Workday public REST API.

ATS identification (Step 1, verified live 2026-09-14, not guessed): Cadence's
tenant lives at ``cadence.wd1.myworkdayjobs.com``, site ``External_Careers``
(confirmed via a direct POST to the standard Workday CXS ``/jobs`` endpoint,
not assumed from a URL-shape guess). India is a genuine, working
``Location_Country`` facet — the same cross-tenant GUID used at Wells
Fargo/Citi (``c4f78be1a8f14da0ab49ce1162348a5e``) narrows the ~601-job global
pool to 188 India jobs, no leakage observed (every checked result's
``locationsText`` was a real Indian city — Noida/Bengaluru/Pune/Hyderabad).
``searchText`` genuinely narrows server-side.

Cadence is an EDA (Electronic Design Automation) software company — unlike
the semiconductor chip-design GCCs already in this repo (AMD/Intel/
Broadcom/TI/Arm), Cadence's own product is software (Virtuoso, Genus,
Innovus, Xcelium, etc.), so its India R&D skews much more heavily toward
compiler/algorithm/verification *software* engineering roles rather than
physical/RTL hardware design. Confirmed live: Cadence's India pool has a
large genuine software-engineer population across multiple non-Pune-only
cities — "Software Engineer II" / "Lead Software Engineer" / "Principal
Software Engineer" repeated dozens of times across Noida, Bengaluru, and
Hyderabad (Pune also present but not the only site, so this is not a
structural Pune-only zero).
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://cadence.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/cadence/External_Careers/jobs"
_JOB_BASE = f"{_BASE_URL}/External_Careers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/cadence/External_Careers"
_PAGE_SIZE = 20

# Same cross-tenant India locationCountry WID seen at Wells Fargo/Citi/TI.
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
    "Referer": f"{_BASE_URL}/External_Careers",
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
        "appliedFacets": {"Location_Country": [_INDIA_WID]},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(_SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Cadence Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Cadence fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(R\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(R\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Unlike Wells Fargo's tenant, Cadence's locationsText never includes
        # the country name (e.g. "NOIDA", "2 Locations") -- it's a bare city
        # or site label. Since results are already server-filtered by the
        # India Location_Country facet, append ", India" rather than reject
        # on a missing "india" substring (which would silently drop every
        # single result).
        if loc and "india" not in loc.lower():
            loc = f"{loc}, India"

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc or "India",
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/External_Careers/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("Cadence description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            return "", ""

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
