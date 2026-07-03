"""Fetches Bank of America job listings via its custom AEM-backed careers site.

careers.bankofamerica.com is an Adobe Experience Manager (AEM) site with an
in-house JSON search servlet, not a third-party ATS (job-detail pages link to
an underlying ghr.wd1.myworkdayjobs.com Workday tenant only for the internal
"acknowledge posting eligibility" popup — the public search and description
never touch Workday).

Key quirks:
- GET /services/jobssearchservlet?start=0&rows=N&search=getAllJobs returns
  every open job (~1760 total) regardless of keyword/country params passed —
  both are silently ignored server-side. The whole pool is cached in-module
  on first call and filtered for country == "India" client-side.
- Job-detail pages (jcrURL) are plain server-rendered HTML — no Playwright
  needed. Description text lives in a
  <div class="job-description-body__internal"> block.
- postedDate is "MM/DD/YYYY"; converted to "YYYY-MM-DD" for matcher.py's
  lexicographic date sort.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE = "https://careers.bankofamerica.com"
_SEARCH_URL = f"{_BASE}/services/jobssearchservlet"
_MAX_ROWS = 5000  # total pool is ~1760; comfortably above that

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_BASE}/en-us/job-search",
}

# Module-level cache: filled once, reused for all keyword calls.
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _parse_date(posted: str) -> str:
    """Convert 'MM/DD/YYYY' -> 'YYYY-MM-DD'."""
    if not posted:
        return ""
    try:
        return datetime.strptime(posted, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _fill_cache(timeout: int = 30) -> None:
    """Fetch the full BofA job pool and cache India postings.

    _cache_filled is set before the try block so a failure doesn't trigger
    a retry storm on every subsequent keyword call (Honeywell lesson).
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL,
                headers=_HEADERS,
                params={"start": 0, "rows": _MAX_ROWS, "search": "getAllJobs"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Bank of America: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Bank of America cache fill failed after 3 attempts: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"Bank of America cache fill: no response — {last_exc}")

    collected: list[dict] = []
    for job in r.json().get("jobsList", []):
        if (job.get("country") or "").strip().lower() != "india":
            continue
        title = (job.get("postingTitle") or "").strip()
        req_id = job.get("jobRequisitionId") or ""
        jcr_url = job.get("jcrURL") or ""
        if not (title and req_id and jcr_url):
            continue
        loc = (job.get("primaryLocation") or "").strip() or "India"
        collected.append({
            "id": req_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_date(job.get("postedDate") or ""),
            "application_url": f"{_BASE}{jcr_url}",
        })

    _india_cache = collected
    print(f"[Bank of America] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Bank of America India jobs.

    Keyword/location are ignored server-side; the cache holds all India jobs
    fetched in one pass. Pagination via start/num slices the cache so
    find_matching_jobs terminates naturally once the slice is empty.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the plain-text job description from a server-rendered detail page."""
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Bank of America description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Bank of America description fetch failed: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"Bank of America description fetch: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.select_one(".job-description-body__internal")
    description = " ".join(body.get_text(separator=" ").split()) if body else ""
    return description, ""
