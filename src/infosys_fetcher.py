"""Fetches Infosys job listings via the custom Infosys Careers REST API.

Infosys runs a bespoke in-house ATS at career.infosys.com (Angular SPA),
backed by a REST gateway at intapgateway.infosysapps.com/careersci/.

Key behavioural note: the searchText param on getCareerSearchJobs is
IGNORED server-side — all ~1500 India lateral+fresher jobs are always
returned regardless of keyword. The module cache below fetches once on the
first keyword call; every subsequent keyword call returns from cache
without making any HTTP request.

ATS discovery path:
  career.infosys.com/assets/js/env.js   → unauthURL (wrong path — uses 'careers')
  career.infosys.com/assets/environments/environment.json
                                         → JobsUnAuthUrl (correct: 'careersci')
  career.infosys.com/main.js             → getCareerSearchJobs, getJobDesc endpoints
"""

from __future__ import annotations

import re
import time
from datetime import datetime

import requests

_BASE_UNAUTH = "https://intapgateway.infosysapps.com/careersci/search/intapjbsrch/"
_SEARCH_URL = f"{_BASE_UNAUTH}getCareerSearchJobs"
_DESC_URL = f"{_BASE_UNAUTH}getJobDesc"
_CAREER_BASE = "https://career.infosys.com"

# sourceId=1  → lateral (experienced) hires — India IL company
# sourceId=21 → fresher hires — India IL company
# Both returned together with sourceId=1,21
_SOURCE_IDS = "1,21"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{_CAREER_BASE}/",
    "Origin": _CAREER_BASE,
}

# ---------------------------------------------------------------------------
# Module-level cache: filled once, reused for all keyword calls.
# Key: referenceCode (e.g. "INFSYS-EXTERNAL-247575")
# Value: raw job dict from the API
# ---------------------------------------------------------------------------
_cache: dict[str, dict] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from the Infosys careers API."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_date(created_on: str) -> str:
    """Convert ISO timestamp '2026-06-25T04:50:17.471' → 'YYYY-MM-DD'."""
    if not created_on:
        return ""
    try:
        return datetime.fromisoformat(created_on.split("T")[0]).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


def _normalize_location(raw: str) -> str:
    """Normalize Infosys city name and append ', India' for the matcher.

    The API returns uppercase city names with trailing spaces:
    'BANGALORE', 'PUNE                     ', 'Gurgaon', etc.
    is_india_job() in matcher.py requires 'india' to appear in the string.
    """
    city = raw.strip().title()
    return f"{city}, India"


def _build_description(job: dict) -> str:
    """Build a plain-text description from list-response fields.

    The list response already contains all skill/role content:
      technicalRequirement  — primary skills and qualifications (clean text)
      rolesResponsibilities — day-in-the-life responsibilities (clean text)
      preferredSkills       — hierarchical skill taxonomy string (clean text)
      genericSkills         — secondary skills (clean text)
      postingTitle, unit    — context

    The additionalResponsibility field has encoding corruption (Unicode U+2022
    bullet characters inserted between every character, a known Infosys API
    defect) and is intentionally omitted; the other fields are sufficient.
    """
    parts = [
        job.get("postingTitle") or "",
        job.get("unit") or "",
        job.get("functionalArea") or "",
        job.get("technicalRequirement") or "",
        job.get("rolesResponsibilities") or "",
        job.get("preferredSkills") or "",
        job.get("genericSkills") or "",
    ]
    return " ".join(p.strip() for p in parts if p.strip())


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the complete India job listing from Infosys and populate the cache.

    Called at most once per process run. _cache_filled is set before the
    network call so that a failure doesn't cause a retry storm on the next
    keyword iteration.
    """
    global _cache_filled
    _cache_filled = True  # Set before try — prevents retry storms on failure

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL,
                headers=_HEADERS,
                params={"sourceId": _SOURCE_IDS, "searchText": "ALL"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Infosys API: 429 rate-limited during cache fill")
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
                f"Infosys cache fill failed after 3 attempts: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"Infosys cache fill: no response — {last_exc}")

    for job in r.json():
        ref = job.get("referenceCode", "").strip()
        if ref:
            _cache[ref] = job


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return Infosys India job listings.

    The Infosys API ignores the keyword parameter and returns all ~1500
    India jobs on every call. The cache is filled once on the first call
    (start=0); subsequent calls with start>0 return [] to signal that
    there are no further pages. matcher.py's deduplication handles the
    repeated full listing across keyword iterations.
    """
    if start > 0:
        return []

    global _cache_filled
    if not _cache_filled:
        _fill_cache(timeout)

    jobs: list[dict] = []
    for ref, job in _cache.items():
        title = (job.get("postingTitle") or "").strip()
        if not title:
            continue
        loc = _normalize_location(job.get("location") or "")
        posting_date = _parse_date(job.get("createdOn") or "")
        source_id = job.get("sourceId", 1)
        # /jobdesc is the SPA's job-description route; /jobdetails is a 404
        app_url = (
            f"{_CAREER_BASE}/jobdesc"
            f"?jobReferenceCode={ref}&sourceId={source_id}"
        )
        jobs.append({
            "id": ref,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single Infosys job.

    Prefers the cached list-response data (no extra HTTP call).
    Falls back to the getJobDesc API if the referenceCode is not in cache
    (e.g. if the cache fill failed silently).
    """
    # Extract referenceCode from URL
    # e.g. https://career.infosys.com/jobdesc?jobReferenceCode=INFSYS-EXTERNAL-247575&sourceId=1
    m = re.search(r"jobReferenceCode=([^&]+)", application_url)
    ref_code = m.group(1) if m else ""

    # Prefer cache — no extra HTTP call
    if ref_code and ref_code in _cache:
        job = _cache[ref_code]
        description = _build_description(job)
        posting_date = _parse_date(job.get("createdOn") or "")
        return description, posting_date

    # Fallback: call getJobDesc API
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _DESC_URL,
                headers=_HEADERS,
                params={"referenceCode": ref_code or application_url},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Infosys getJobDesc: 429 rate-limited")
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
                f"Infosys description fetch failed: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(
            f"Infosys getJobDesc: no response — {last_exc}"
        )

    data = r.json()
    parts = [
        data.get("postingTitle") or "",
        data.get("techRequirement") or "",
        data.get("rolesResponsibilities") or "",
        data.get("preferredSkills") or "",
    ]
    description = " ".join(p.strip() for p in parts if p.strip())
    posting_date = _parse_date(data.get("createdOn") or "")
    return description, posting_date
