"""Fetches TransUnion CIBIL job listings via the parent TransUnion Workday tenant.

TransUnion CIBIL is TransUnion's Indian credit-bureau joint venture -- a
distinct legal/hiring entity from global TransUnion, headquartered at
"One World Center", Mumbai (visible directly in this tenant's own
locationsText: "Mumbai - One World Center"). Investigated from scratch
2026-09-08: transunioncibil.com's own careers page is bot-protected
(plain `requests` gets HTTP 403), but probing common Workday tenant
slugs found `transunion.wd5.myworkdayjobs.com/TransUnion` responds with
real job data (HTTP 200) -- i.e. CIBIL genuinely shares its parent's
Workday tenant rather than running a separate ATS. This fetcher is
scoped to India postings only (via the standard cross-tenant India
`locationCountry` WID `c4f78be1a8f14da0ab49ce1162348a5e`, same GUID
already used by Wells Fargo/Citi/Fidelity/Sprinklr elsewhere in this
repo) -- since virtually all of TransUnion's India-based hiring IS the
CIBIL entity, this India-only slice is a faithful CIBIL pipeline without
pulling in the much larger US/UK/LatAm parent-company job pool. Slug is
`transunioncibil` (not `transunion`) precisely so a future, separate
global-TransUnion integration cannot collide with this one.

`searchText` genuinely narrows server-side (confirmed live: a nonsense
token returns total=0; ".NET" returns a strict subset). `locationsText`
on this tenant omits the country ("Bengaluru", "Pune", "Mumbai - One
World Center", "Mumbai - INDAS", "Remote - Mumbai", or vague "N
Locations") -- same shape as Invesco/Finastra -- so India is confirmed
via the already-applied facet and ", India" is appended for matcher.py's
is_india_job() check. Pune/Chennai are NOT filtered out here; they're
still tagged India so matcher.py's exclude_locations layer (which needs
the city name present) can reject them -- live board currently has
genuine "Lead/Sr Developer, C# .NET & APIs" postings, both in Pune
(correctly excluded), so this distinction matters here more than in most
companies in this repo.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://transunion.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/transunion/TransUnion/jobs"
_JOB_BASE = f"{_BASE_URL}/TransUnion"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/transunion/TransUnion"

_PAGE_SIZE = 20

# Cross-tenant India locationCountry WID -- also used by Wells Fargo, Citi,
# Fidelity, Northern Trust, MUFG, Shell, Sprinklr, Guidewire elsewhere here.
_INDIA_COUNTRY_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/TransUnion",
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
        "appliedFacets": {"locationCountry": [_INDIA_COUNTRY_WID]},
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
                raise RateLimitError("TransUnion CIBIL Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"TransUnion CIBIL fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = p.get("locationsText", "").strip()
        # Facet already scopes this request to India; tenant just omits the
        # country word from locationsText, so append it for the matcher.
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
    The startDate field in the detail response is already ISO (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/TransUnion/", 1)[-1]
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
                raise RateLimitError("TransUnion CIBIL description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"TransUnion CIBIL description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
