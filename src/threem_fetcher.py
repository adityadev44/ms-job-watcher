"""Fetches 3M job listings via the Workday public REST API.

ATS discovery (live, 2026-09-14): 3M's careers site is Workday, hosted at
``3m.wd1.myworkdayjobs.com``, site ``Search``. Confirmed via a plain POST
to the CXS endpoint -- no browser needed, same shape as Wells Fargo/
Accenture. Note: the registry slug is ``threem`` (not ``3m``) because a
leading digit is not a valid Python module-name character -- "3m" is only
used in the display source string / config comments.

India is filtered server-side using the ``Location_Country`` facet, WID
``c4f78be1a8f14da0ab49ce1162348a5e`` -- the same cross-tenant GUID already
seen at Wells Fargo/Citi/Caterpillar. Verified live: 108 total India jobs,
pagination (offset 0/20/40/60/80/100) advances cleanly with no
UBS/MUFG/Nvidia-style wraparound and ``total`` stays consistent across
pages (unlike Caterpillar's tenant) -- still capped defensively below.

Locations arrive in several different shapes on this tenant, e.g.
"IN, BANGALORE", "IN, Bangalore Kar" (a truncated "Karnataka"),
"IN, Maharashtra, Pune", "IND, GURGAON", or bare "2 Locations"/"3
Locations" for multi-site postings -- none contain the literal substring
"india", so ``", India"`` is appended before handing off to matcher.py
(same fallback pattern as Invesco/Continental). City/state text is
preserved so exclude_locations (Pune/Chennai/Tamil Nadu/etc.) still works
correctly; ambiguous "N Locations" postings are treated as bare "India"
(same accepted-precision-gap class as Maersk's "2 Locations" jobs -- safe
because the whole pool is already pre-filtered by the India WID).

Real non-Pune/non-Chennai software roles confirmed live in Bangalore:
"Senior Workday Developer", "Senior Engineer - DevOps", "MuleSoft
Integration Architect", "SAP-BTP Tech Lead", "Application Development
Engineer - Electronics" -- genuine India engineering presence, not just
sales/manufacturing.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://3m.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/3m/Search/jobs"
_JOB_BASE = f"{_BASE_URL}/Search"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/3m/Search"
_PAGE_SIZE = 20
_MAX_PAGES = 20  # safety cap (~400 jobs), well beyond the known ~108-job India pool

# India locationCountry WID -- same cross-tenant GUID as Wells Fargo/Citi/Caterpillar.
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
    "Referer": f"{_BASE_URL}/Search",
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


def _normalize_location(loc: str) -> str:
    loc = (loc or "").strip()
    if not loc:
        return "India"
    if "india" not in loc.lower():
        loc = f"{loc}, India"
    return loc


def _request(method: str, url: str, *, json_body: dict | None = None, timeout: int = 20):
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if method == "POST":
                    r = requests.post(url, headers=_HEADERS, json=json_body, timeout=timeout, verify=False)
                else:
                    r = requests.get(url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("3M Workday: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"3M fetch failed: {exc}") from exc
    raise RateLimitError("3M fetch failed: exhausted retries")


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
    r = _request("POST", _SEARCH_URL, json_body=body, timeout=timeout)

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
        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": _normalize_location(loc),
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/Search/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = _request("GET", api_url, timeout=timeout)
    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""
    return description, posting_date
