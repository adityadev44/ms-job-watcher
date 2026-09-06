"""Fetches Aon job listings via the iCIMS-backed public REST API at jobs.aon.com.

Aon's public careers portal (jobs.aon.com) is built on Jibe/iCIMS
infrastructure. The job-search API endpoint (`/api/jobs`) is a plain
unauthenticated GET that returns structured JSON — no Playwright needed.

Confirmed live during onboarding (2026-09-06):

- **Location filter**: `location=India` reliably restricts results to India
  (42 total India jobs). No server-side country-code needed.
- **Keyword filter**: `keywords=<term>` genuinely narrows server-side
  (verified A/B: `keywords=software+engineer` → 4 results;
  `keywords=xyznonsensequeryabc` → 0; no keyword → 42). NOT in
  `_IGNORES_KEYWORDS`.
- **Pagination**: The `page` parameter is 1-indexed; `limit` controls page
  size (tested up to 100). Standard `offset` does not work on this tenant —
  only `page` advances the result window. `start` in the fetcher contract
  maps to page number via `page = start // num + 1`. Returns an empty
  `jobs` list cleanly at/beyond the last page — no wraparound observed.
- **Descriptions**: inline in every API response as HTML in `data.description`
  and `data.responsibilities`. `fetch_job_description` raises
  `NotImplementedError("descriptions are inline")` accordingly.
- **Apply URL**: `data.apply_url` → `india-careers-aon.icims.com/jobs/{id}/login`.
  The `/login` suffix redirects unauthenticated users to the job-detail page.
  The fetcher also stores a canonical `jobs.aon.com/jobs/{id}` URL that
  resolves to the public listing.
- **Date format**: ISO8601 string, e.g. "2026-07-02T06:00:00+0000" → first
  10 characters = "2026-07-02".

India coverage (2026-09-06): 42 jobs total — mix of actuarial consulting,
reinsurance, HR tech, and engineering roles (e.g. "Full Stack Engineer",
"Quality Assurance").  Most non-tech roles still serve as a useful breadth
signal for our title/skills matcher; the small pool size means the full
42-job sweep is fast and cheap.
"""

from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://jobs.aon.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"
_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/jobs",
}

# Pagination-guard state: page-1 first job IDs, keyed by keyword.
# The Aon iCIMS API returns an empty list cleanly past the last page, so
# wraparound is not observed in practice — but guard is kept for consistency.
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent request failure from jobs.aon.com."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Return 'YYYY-MM-DD' from an ISO8601 string; '' on failure."""
    return raw[:10] if raw and len(raw) >= 10 else ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Aon India job postings.

    `keyword` genuinely narrows results server-side (`keywords` param).
    `location` is sent as `location=India` — the API's own location param.
    `start` is an absolute offset; converted to a 1-indexed page number via
    ``page = start // num + 1``.
    """
    global _FIRST_PAGE_IDS

    page = start // num + 1
    params = {
        "keywords": keyword,
        "location": "India",
        "limit": num,
        "page": page,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Aon (jobs.aon.com): 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Aon search failed after 3 attempts: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Aon search: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Aon search returned non-JSON: {exc}") from exc

    raw_jobs = payload.get("jobs", [])

    jobs: list[dict[str, str]] = []
    for item in raw_jobs:
        jd = item.get("data", {})

        job_id = str(jd.get("req_id") or "").strip()
        if not job_id:
            continue

        title = (jd.get("title") or "").strip()
        if not title:
            continue

        # Build location string from city + country fields.
        city = (jd.get("city") or "").strip()
        country = (jd.get("country") or "India").strip()
        if city:
            loc_str = f"{city}, {country}"
        else:
            loc_str = country
        # Guard: skip non-India rows (shouldn't happen with location=India filter).
        if "india" not in loc_str.lower():
            continue

        # URL: use the iCIMS apply link (redirects to job detail for anonymous users).
        apply_url = (jd.get("apply_url") or "").strip()
        if not apply_url:
            apply_url = f"{_BASE_URL}/jobs/{job_id}"

        # Description is inline (HTML in data.description + data.responsibilities).
        desc_html = (jd.get("description") or "") + " " + (jd.get("responsibilities") or "")
        description = _strip_html(desc_html)

        posting_date = _parse_date(jd.get("posted_date", ""))

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc_str,
            "application_url": apply_url,
            "posting_date": posting_date,
            "description": description,
        })

    # Pagination guard.
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []  # wraparound detected

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Descriptions are included inline in the search response."""
    raise NotImplementedError("descriptions are inline")
