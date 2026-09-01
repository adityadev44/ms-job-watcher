"""Fetches Automation Anywhere job listings via the Workday public REST API.

Automation Anywhere's ATS is Workday, hosted at
automationanywhere.wd5.myworkdayjobs.com, site "AutomationAnywhereJobs"
(discovered via the `<script src="...">` embed on
https://www.automationanywhere.com/company/careers/all -- the marketing page
itself is plain server-rendered HTML with a Workday iframe/script embed, no
Playwright needed anywhere in this pipeline).

Standard Workday CXS REST shape, same as Wells Fargo/Citi/Fidelity/etc. --
plain POST, no browser required.

India is filtered server-side via the `locationCountry` facet, nested under
`locationMainGroup` (same nesting shape as Automation Anywhere's fellow
agentic-automation peer UiPath... no wait, UiPath is Ashby; the nesting shape
actually matches Adobe/WTW/Fiserv/Visa). India WID
`c4f78be1a8f14da0ab49ce1162348a5e` is the SAME cross-tenant GUID reused by
Fidelity/Wells Fargo/Citi/Northern Trust/MUFG/Thomson Reuters -- this repo's
Nth confirmation that Workday reuses this GUID for India across unrelated
tenants. Verified live 2026-08-31: 13 of 35 total global postings are India
(all Bengaluru).

Job ID: bulletFields carries a single element in this tenant's shape, the bare
requisition ID ("JR1496" -- note the "JR" prefix, NOT "R-" like Wells Fargo).
Falls back to parsing the externalPath's trailing "_JR<digits>" segment if
bulletFields is ever empty.

Titles/descriptions carry real signal -- this is a small, genuine AI-native
product company's India engineering centre, not an IT-services shop with
generic level-banded titles. Live sample: "Sr. AI Engineer" (Bengaluru)
explicitly names LangChain/LangGraph/CrewAI/RAG/vector database/Python in its
description body -- a clean AI/ML/Python match. require_tech_in_description
is therefore NOT enabled, consistent with other direct-employer product
companies (UiPath, LSEG, Broadridge) rather than IT-services shops.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://automationanywhere.wd5.myworkdayjobs.com"
_TENANT = "automationanywhere"
_SITE = "AutomationAnywhereJobs"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"
_PAGE_SIZE = 20

# India locationCountry WID -- same cross-tenant GUID reused across many
# Workday tenants in this repo (Wells Fargo, Citi, Fidelity, Northern Trust,
# MUFG, Thomson Reuters, ...). Verified live 2026-08-31.
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
    "Referer": f"{_JOB_BASE}",
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
        "limit": min(num, _PAGE_SIZE),
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
                raise RateLimitError("Automation Anywhere Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Automation Anywhere fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(JR\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
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
        if "india" not in loc.lower():
            # Safety net -- facet already restricts to India, but a tenant
            # config change could silently drop that filter.
            continue

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc or "India",
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    application_url is a browsable HTML page; transformed to the JSON API
    path under the same CXS prefix.
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Automation Anywhere description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Automation Anywhere description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
