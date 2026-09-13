"""Fetches Priceline (Booking Holdings) job listings via the Workday public REST API.

Priceline's own branded careers site (careers.priceline.com) is a WordPress
site (WP Engine) whose job-search results and detail pages are fully
server-rendered HTML — but every "Apply Now" link on those pages resolves to
plain Workday: tenant `priceline`, site `Priceline`, hosted at
priceline.wd1.myworkdayjobs.com. Rather than scrape the WordPress front end,
this fetcher calls the underlying Workday CXS API directly (found by reading
the real "Apply Now" destination on a live job page, not guessed from the
domain), which is both simpler and gives a real JSON detail payload for
descriptions/dates instead of parsing rendered HTML.

Small tenant (~65 total live reqs globally, confirmed 2026-09-13). No
`locationCountry` facet is exposed (probing the `/facets` endpoint returns
HTTP 405 — not supported on this tenant), so this fetcher always fetches the
full per-keyword pool and lets `locationsText` (plus one detail-page GET for
ambiguous "N Locations" postings) decide India membership — same
conservative pattern as Duck Creek Technologies. `searchText` genuinely
narrows server-side (confirmed live: "" -> 65 total, "engineer" -> 36), so
this is NOT registered in `_IGNORES_KEYWORDS`.

Priceline's only current India office is Mumbai (confirmed against the
WordPress site's own `_job_location` facet list, which has no other Indian
city). A handful of postings show as "2 Locations" in the search result with
no readable city text; every one checked so far pairs Norwalk/CT, New York,
or Toronto — never Mumbai — but this fetcher still resolves them via one
extra detail-page GET per posting (the whole tenant is tiny, so the cost is
negligible) rather than assume that will always hold.

`limit` > 20 returns HTTP 400 (same family as Northern Trust/Duck Creek), so
this fetcher hard-caps `num` at 20 per request; matcher.py's own pagination
loop already calls with num=20 and increasing `start` until an empty page.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_TENANT_HOST = "https://priceline.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_TENANT_HOST}/wday/cxs/priceline/Priceline/jobs"
_DETAIL_BASE = f"{_TENANT_HOST}/wday/cxs/priceline/Priceline"
_PUBLIC_JOB_BASE = f"{_TENANT_HOST}/Priceline"

_PAGE_SIZE = 20

# Word-boundary India check -- never matches "Indianapolis"/"Indiana".
_INDIA_RE = re.compile(r"\bindia\b|\bmumbai\b", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_TENANT_HOST}/Priceline",
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
    """Extract the R##### requisition ID from a search-result posting."""
    for field in posting.get("bulletFields", []) or []:
        m = re.match(r"^(R\d+)$", str(field).strip(), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    m = re.search(r"_(R\d+)(?:-\d+)?$", posting.get("externalPath", ""), re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return ""


def _request_with_retry(method, url, *, json_body=None, timeout=20, error_label="Priceline"):
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if method == "post":
                    r = requests.post(url, headers=_HEADERS, json=json_body, timeout=timeout)
                else:
                    r = requests.get(url, headers=_HEADERS, timeout=timeout)
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
        r = _request_with_retry("get", api_url, timeout=timeout, error_label="Priceline location resolve")
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

    r = _request_with_retry("post", _SEARCH_URL, json_body=body, timeout=timeout, error_label="Priceline search")

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

        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "Mumbai, India"

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
    elif "/Priceline/" in application_url:
        ext_path = "/" + application_url.split("/Priceline/", 1)[-1]
    else:
        m = re.search(r"/job/.*$", application_url)
        if m:
            ext_path = m.group(0)

    if not ext_path:
        return "", ""

    api_url = f"{_DETAIL_BASE}{ext_path}"
    r = _request_with_retry("get", api_url, timeout=timeout, error_label="Priceline description")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or _parse_posted_on(info.get("postedOn", ""))

    return description, posting_date
