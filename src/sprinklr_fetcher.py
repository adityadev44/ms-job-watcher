"""Fetches Sprinklr job listings via the Workday public REST API.

Sprinklr's ATS is Workday, hosted at sprinklr.wd1.myworkdayjobs.com with site
code "careers" (confirmed via the public careers page's own outbound links to
sprinklr.wd1.myworkdayjobs.com/careers?jobFamilyGroup=... 2026-09-08). The
Workday CXS endpoint accepts plain POST requests -- no browser needed, same
shape as Wells Fargo (src/wellsfargo_fetcher.py).

India is filtered server-side using the locationCountry facet WID
"c4f78be1a8f14da0ab49ce1162348a5e" -- the same cross-tenant GUID already
documented working for Wells Fargo/Citi in this repo. Confirmed live: 32
India postings (Gurgaon, Haryana and Bangalore, Karnataka -- Sprinklr's real
India hiring hubs), keyword search genuinely narrows server-side (searchText
"engineer" cut the India pool from 32 to 21).

Some postings show locationsText == "2 Locations" instead of a real city --
same ambiguous-multi-site shape documented for Maersk/SimCorp in the
playbook. Since the search itself is already scoped to the India
locationCountry facet, these are safely still India; the fetcher normalizes
ambiguous text to "India" so matcher.py's location check still passes, and
additionally recovers the real city from the externalPath's leading
"India---State---City" segment when present, so Pune/Chennai/etc. exclusions
still work correctly even on jobs Workday's own facet groups as "2 Locations".
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://sprinklr.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/sprinklr/careers/jobs"
_JOB_BASE = f"{_BASE_URL}/careers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/sprinklr/careers"
_PAGE_SIZE = 20

# India locationCountry WID -- same stable cross-tenant GUID as Wells Fargo/Citi.
# Verified 2026-09-08: returns 32 India results with no keyword.
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


# Pagination wraps around once offset reaches the true total (confirmed live:
# offset=32 on a 32-job India pool returns page 1 again instead of an empty
# list, same wraparound family as UBS/Nvidia/Walmart in this repo) --
# module-level dedup across the whole process run stops matcher.py's loop
# cleanly: once a page's jobs have all already been returned, it comes back
# empty and matcher.py's `if not page: break` ends pagination for that
# keyword/location combo.
_seen_job_ids: set[str] = set()


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


def _location_from_external_path(external_path: str) -> str:
    """Recover "City, State, India" from a "/job/India---State---City/..." path."""
    m = re.match(r"^/job/India---([^/]+)---([^/]+)/", external_path)
    if not m:
        m = re.match(r"^/job/India---([^/]+)/", external_path)
        if m:
            return f"{m.group(1).replace('-', ' ')}, India"
        return ""
    state, city = m.group(1).replace("-", " "), m.group(2).replace("-", " ")
    return f"{city}, {state}, India"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    # Workday rejects limit > 20 with a plain HTTP 400 (confirmed live:
    # limit=20 succeeds, limit=25 and above all 400) -- clamp defensively,
    # same discipline as every other Workday tenant in this repo.
    body = {
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
        "limit": min(num, 20),
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
                raise RateLimitError("Sprinklr Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Sprinklr fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(\d+-JOB(?:-\d+)?)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(\d+-JOB(?:-\d+)?)$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue
        if job_id in _seen_job_ids:
            # Already returned in an earlier page this run -- either a true
            # duplicate or a wraparound page. Skip so a wrapped page comes
            # back fully empty and matcher.py's pagination loop terminates.
            continue
        _seen_job_ids.add(job_id)

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        if "india" not in loc.lower():
            # Ambiguous "N Locations" text -- already India-scoped via the
            # server-side facet; recover a real city from externalPath when
            # possible so exclude_locations can still act on it.
            loc = _location_from_external_path(external_path) or "India"

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
        ext_path = "/" + application_url.split("/careers/", 1)[-1]
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
                raise RateLimitError("Sprinklr description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Sprinklr description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
