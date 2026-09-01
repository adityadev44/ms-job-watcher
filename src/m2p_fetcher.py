"""Fetches M2P Fintech job listings via its own custom Next.js career-site API.

ATS discovery (2026-08-31): www.m2pfintech.com/careers 301-redirects to a
dedicated subdomain, careers.m2pfintech.com — a Next.js app. The page's own
static HTML has zero job data or search links (fully client-rendered), and
the obvious `/_next/data/{buildId}/view-jobs.json` route only ever returns
an empty `{}` (that specific route is used for Next.js's Link-prefetch
mechanism, not real data). The real API — found via a live Playwright
network capture of the rendered page — is:

    GET https://careers.m2pfintech.com/api/job-list/
        -> {"status": 1, "data": [{job_id, job_code, job_title,
             location_city, location_country, department,
             parent_department, employee_type, job_created_timestamp,
             job_updated_timestamp, ...}]}   (no description field)

    GET https://careers.m2pfintech.com/api/job-details/?job_id={id}
        -> {"status": 1, "data": {job_title, location_city, job_decription
             (sic — HTML), ...}}

No keyword/pagination params — the whole board (confirmed live: 15 total
postings) comes back in one `job-list` call. Small board, mostly Chennai
(M2P's HQ, and one of this watcher's excluded cities); 4 non-Chennai
postings observed (Hyderabad ×3 engineering/solution-engineering roles,
Mumbai ×1 sales).

**Sharp edge, worth flagging for future companies on this same custom
stack:** `job-details` (but not `job-list`) returns HTTP 500
`{"status":0,"message":"IP not allowed"}` when called from Playwright/
headless-Firefox's network stack, but returns a normal 200 with full data
from a plain `requests.get()` carrying a browser-like User-Agent + a
`Referer` header pointing at the job-description page. This is the
opposite of the usual "Playwright bypasses bot-blocks that plain requests
can't" pattern seen elsewhere in this repo (Honeywell/IBM/BNP Paribas) —
here the WAF rule appears to be flagging Playwright's own TLS/HTTP2
fingerprint specifically, not blocking scripted traffic in general. Always
try plain `requests` first even after finding an API via Playwright.

Description quirk: `job_decription` is HTML-entity-escaped one extra level
(raw text starts `&lt;div&gt;...`), same idiom as Razorpay/Groww/Zomato —
unescape, strip tags, unescape again for inline entities.

Application URL: `https://careers.m2pfintech.com/job-description/{job_id}/`
— confirmed live via Playwright (this is the exact route the site's own
job cards link to).
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests

_API_BASE = "https://careers.m2pfintech.com/api"
_JOB_LIST_URL = f"{_API_BASE}/job-list/"
_JOB_DETAILS_URL = f"{_API_BASE}/job-details/"
_JOB_PAGE_BASE = "https://careers.m2pfintech.com/job-description/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://careers.m2pfintech.com/view-jobs/",
}

# Module-level cache: the job-list endpoint has no query params and always
# returns the full board, so fetch once per process.
_job_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _epoch_date(ts: str) -> str:
    """job_created_timestamp is "DD-MM-YYYY HH:MM:SS"; convert to YYYY-MM-DD."""
    if not ts:
        return ""
    try:
        return datetime.strptime(ts.split(" ")[0], "%d-%m-%Y").strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return ""


def _location_from_job(j: dict) -> str:
    cities = j.get("location_city") or []
    city = (cities[0] if cities else "").strip()
    country = (j.get("location_country") or "").strip()
    if city and country:
        if country.lower() in city.lower():
            return city
        return f"{city}, {country}"
    return city or country or "India"


def _get_json(url: str, *, params: dict | None = None, timeout: int = 20, context: str = "") -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"M2P {context}: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"M2P {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"M2P {context}: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire M2P job-list board once and cache it.

    _cache_filled is set before the fetch attempt so a transient failure
    doesn't retry-storm on every subsequent fetch_jobs() call in the same
    process (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    data = _get_json(_JOB_LIST_URL, timeout=timeout, context="job list")
    raw_jobs = (data or {}).get("data") or []

    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("job_id") or "").strip()
        title = (j.get("job_title") or "").strip()
        if not (job_id and title):
            continue
        collected.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(j),
            "posting_date": _epoch_date(j.get("job_created_timestamp") or ""),
            "application_url": f"{_JOB_PAGE_BASE}{job_id}/",
        })

    _job_cache[:] = collected
    print(f"[M2P Fintech] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of M2P Fintech jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    the job-list endpoint has no query params and always returns the full
    board; the shared matcher does the real title/skill/India filtering.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single M2P Fintech job.

    A separate detail call is required — job-list does not include
    descriptions. See module docstring for the "IP not allowed" quirk this
    endpoint has under Playwright but not under plain requests.
    """
    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]
    data = _get_json(
        _JOB_DETAILS_URL,
        params={"job_id": job_id},
        timeout=timeout,
        context=f"job detail {job_id}",
    )
    detail = (data or {}).get("data") or {}
    description = _strip_html(detail.get("job_decription") or detail.get("job_description") or "")

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
