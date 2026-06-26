"""Fetches TCS job listings via the iBegin proprietary REST API.

TCS's ATS is iBegin, a proprietary Angular SPA hosted at:
  https://ibegin.tcsapps.com/candidate/

This portal is India-specific (countryId=101 is embedded in the page HTML).
All jobs returned are India jobs — no location filter is needed server-side.

Search endpoint: POST /candidate/api/v1/jobs/searchJ
  - Body fields discovered by reverse-engineering JobSearchController.js
    (resources/app/components/jobs/job-search/JobSearchController.js)
  - 10 results per page; pageNumber is a string ("1", "2", …)
  - userText is a loose keyword pre-filter — returns noisy results
    ("C#" returns all 4 227 jobs; ".NET" returns 625 including Network roles)
    Treat keywords as hints only; the shared matcher title/skill checks
    do the real filtering.
  - All jobs are India; location is city-name only (e.g. "Bengaluru");
    appended with ", India" so matcher's is_india_job() check passes.

Description endpoint: POST /candidate/api/v1/job/desc
  - Body: {"jobId": <int>}  (strip J/W suffix from search ID, cast to int)
  - Walk-in jobs (ID ends "W"): use /candidate/api/v1/job/desc/walkin
  - Returns HTML description + skilldetail + qualifications

Application URL: https://ibegin.tcsapps.com/candidate/#!/jobs/{id}
  where {id} includes the J/W suffix.

Posting date: iBegin does not expose a posting date. The description
returns applyby ("YYYY-MM-DD HH:MM:SS"); we use its date portion as
a proxy posting date.
"""

from __future__ import annotations

import math
import re
import time
import warnings

import requests

_BASE = "https://ibegin.tcsapps.com/candidate"
_SEARCH_URL = f"{_BASE}/api/v1/jobs/searchJ"
_DESC_URL = f"{_BASE}/api/v1/job/desc"
_DESC_WALKIN_URL = f"{_BASE}/api/v1/job/desc/walkin"
_APP_BASE = f"{_BASE}/#!/jobs"

# Fixed page size returned by TCS iBegin API
_TCS_PAGE_SIZE = 10

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://ibegin.tcsapps.com/candidate/#!/jobs/search",
    "Origin": "https://ibegin.tcsapps.com",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from TCS iBegin."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return " ".join(text.split())


def _parse_applyby(raw: str) -> str:
    """'2026-07-31 00:00:00' → '2026-07-31'.  Returns '' on failure."""
    if raw and len(raw) >= 10:
        return raw[:10]
    return ""


def _make_search_body(keyword: str, page_number: int) -> dict:
    """Build the requestDataObj that JobSearchController.js sends."""
    return {
        "jobTitle": None,
        "jobCity": None,
        "jobFunction": None,
        "jobExperience": None,
        "jobSkill": None,
        "pageNumber": str(page_number),
        "userText": keyword,
        "jobTitleOrder": None,
        "jobCityOrder": None,
        "jobFunctionOrder": None,
        "jobExperienceOrder": None,
        "applyByOrder": None,
        "regular": True,
        "walkin": True,
    }


def _is_walkin(job_id: str) -> bool:
    return job_id.upper().endswith("W")


def _numeric_id(job_id: str) -> int:
    """Strip J/W suffix and return numeric part as int."""
    return int(job_id[:-1])


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
    """Fetch TCS India jobs from iBegin searchJ endpoint.

    All jobs are India — location param is ignored (portal is India-only).
    Pagination: iBegin uses 10-per-page with a string pageNumber field.
    We map the matcher's (num, start) pair to the right TCS pages.
    """
    # First TCS page that covers our start offset (1-indexed)
    tcs_page_start = (start // _TCS_PAGE_SIZE) + 1
    # How many TCS pages we need to cover `num` results
    tcs_pages_needed = max(1, math.ceil(num / _TCS_PAGE_SIZE))

    collected: list[dict] = []

    for p in range(tcs_page_start, tcs_page_start + tcs_pages_needed):
        body = _make_search_body(keyword, p)

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
                    raise RateLimitError("TCS iBegin: 429 rate-limited")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"TCS iBegin fetch failed: {exc}") from exc

        data = r.json()
        if data.get("result") != "Y":
            break

        page_jobs = data.get("data", {}).get("jobs", []) or []

        for j in page_jobs:
            job_id = (j.get("id") or "").strip()
            if not job_id:
                continue

            title = (j.get("jobTitle") or "").strip()
            if not title:
                continue

            # Location is city-name only; append ", India" so matcher passes
            city = (j.get("location") or "").strip()
            loc = f"{city}, India" if city else "India"

            collected.append({
                "id": job_id,
                "title": title,
                "location": loc,
                "posting_date": "",  # not exposed in search; set in fetch_job_description
                "application_url": f"{_APP_BASE}/{job_id}",
            })

        # If we got fewer than a full page, we're at the last page
        if len(page_jobs) < _TCS_PAGE_SIZE:
            break

    return collected[:num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch full job description from iBegin detail API.

    Derives the numeric jobId from the application URL (strip trailing J/W).
    Returns (description_text, posting_date).
    posting_date is the apply-by date in YYYY-MM-DD format (best proxy
    available from this API — iBegin does not expose a posting date).
    """
    # Extract the raw job ID from the URL (ends in J or W)
    raw_id = application_url.rstrip("/").split("/")[-1]
    try:
        numeric_id = _numeric_id(raw_id)
    except (ValueError, IndexError):
        return "", ""

    is_wk = _is_walkin(raw_id)
    desc_url = _DESC_WALKIN_URL if is_wk else _DESC_URL

    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    desc_url,
                    headers=_HEADERS,
                    json={"jobId": numeric_id},
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                raise RateLimitError("TCS iBegin description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"TCS iBegin description fetch failed: {exc}"
            ) from exc

    detail = r.json()
    if detail.get("result") != "Y" or not detail.get("data"):
        return "", ""

    d = detail["data"]

    # Combine HTML description + skills + qualifications into plain text
    parts = []
    desc_html = d.get("description") or ""
    if desc_html:
        parts.append(_strip_html(desc_html))
    skill_detail = d.get("skilldetail") or ""
    if skill_detail:
        parts.append(skill_detail)
    qualifications = d.get("qualifications") or ""
    if qualifications:
        parts.append(qualifications)

    description = " ".join(parts)
    posting_date = _parse_applyby(d.get("applyby") or "")

    return description, posting_date
