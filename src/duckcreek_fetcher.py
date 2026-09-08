"""Fetches Duck Creek Technologies job listings via the Workday public REST API.

Duck Creek's ATS is Workday, tenant `duckcreek`, site `duckcreekcareers`,
hosted at duckcreek.wd1.myworkdayjobs.com (found via outbound job links
embedded on www.duckcreek.com/careers/). No browser needed.

This is a small tenant (~23 total live reqs, confirmed 2026-09-08). No
`locationCountry` facet exists, so this fetcher always fetches the full
pool per keyword and lets the India country field on each job detail (plus
a text safety net on `locationsText`) do the real scoping -- same
conservative pattern as Clearwater Analytics.

`searchText` genuinely narrows server-side (confirmed: "engineer" cut the
23-job pool to 13).

Quirk unique to this tenant: many postings are dual-located (e.g. "Mumbai,
India" + "Bengaluru, India") and the search-result `locationsText` field
just says "2 Locations" with no readable city text in that case -- the
real per-job location only appears in the job detail payload
(`location` + `additionalLocations`). Since matcher.py's own Layer-1 India
check does a literal `"india" in job["location"].lower()` substring test,
a bare "2 Locations" string would silently and incorrectly fail that
check. This fetcher resolves ambiguous multi-location postings via one
extra detail-page GET per posting (the whole tenant is tiny, so the extra
calls are cheap) and joins them into a single "City, India; City2, India"
string.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_TENANT_HOST = "https://duckcreek.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_TENANT_HOST}/wday/cxs/duckcreek/duckcreekcareers/jobs"
_DETAIL_BASE = f"{_TENANT_HOST}/wday/cxs/duckcreek/duckcreekcareers"
_PUBLIC_JOB_BASE = f"{_TENANT_HOST}/duckcreekcareers"

_PAGE_SIZE = 20

# Word-boundary India check -- never matches "Indianapolis"/"Indiana".
_INDIA_RE = re.compile(r"\bindia\b", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_TENANT_HOST}/duckcreekcareers",
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


def _job_id_from_posting(posting: dict) -> str:
    """Extract the REQID requisition ID from a search-result posting."""
    for field in posting.get("bulletFields", []) or []:
        m = re.match(r"^(REQID\d+)$", str(field).strip(), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    m = re.search(r"_(REQID\d+(?:-\d+)?)$", posting.get("externalPath", ""), re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return ""


def _request_with_retry(method, url, *, json_body=None, timeout=20, error_label="Duck Creek"):
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if method == "post":
                    r = requests.post(url, headers=_HEADERS, json=json_body, timeout=timeout, verify=False)
                else:
                    r = requests.get(url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"{error_label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"{error_label} fetch failed: {exc}") from exc
    return r


def _resolve_multi_location(ext_path: str, timeout: int) -> str:
    """Resolve a "N Locations" posting to real city text via the detail API."""
    api_url = f"{_DETAIL_BASE}{ext_path}"
    try:
        r = _request_with_retry("get", api_url, timeout=timeout, error_label="Duck Creek location resolve")
    except RateLimitError:
        return ""
    info = r.json().get("jobPostingInfo", {})
    locs = []
    primary = info.get("location", "")
    if primary:
        locs.append(primary)
    locs.extend(info.get("additionalLocations", []) or [])
    return "; ".join(locs)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    # Workday rejects limit > 20 for this tenant with HTTP 400.
    limit = min(max(int(num), 1), _PAGE_SIZE)

    body = {
        "appliedFacets": {},
        "limit": limit,
        "offset": start,
        "searchText": keyword or "",
    }

    r = _request_with_retry("post", _SEARCH_URL, json_body=body, timeout=timeout, error_label="Duck Creek search")

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = _job_id_from_posting(p)
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()

        # Ambiguous "N Locations" text carries no readable city -- resolve
        # via the job detail payload before deciding India membership.
        if re.match(r"^\d+\s+Locations$", loc, re.IGNORECASE):
            resolved = _resolve_multi_location(external_path, timeout)
            if resolved:
                loc = resolved

        if not _INDIA_RE.search(loc):
            continue

        app_url = f"{_PUBLIC_JOB_BASE}{external_path}" if external_path else ""

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
    ext_path = ""
    if _PUBLIC_JOB_BASE in application_url:
        ext_path = application_url[len(_PUBLIC_JOB_BASE):]
    elif "/duckcreekcareers/" in application_url:
        ext_path = "/" + application_url.split("/duckcreekcareers/", 1)[-1]
    else:
        m = re.search(r"/job/.*$", application_url)
        if m:
            ext_path = m.group(0)

    if not ext_path:
        return "", ""

    api_url = f"{_DETAIL_BASE}{ext_path}"
    r = _request_with_retry("get", api_url, timeout=timeout, error_label="Duck Creek description")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or _parse_posted_on(info.get("postedOn", ""))

    return description, posting_date
