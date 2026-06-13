"""
S&P Global Careers job fetcher — iCIMS REST API (careers.spglobal.com).

This is a SEPARATE portal from spglobal_fetcher.py (which uses the Workday
instance at spgi.wd5.myworkdayjobs.com). The two portals carry different
job sets with non-overlapping req_id namespaces.

The iCIMS API at /api/jobs returns full job descriptions in the search
response, so fetch_job_description() is served from an in-module cache
built during fetch_jobs() — no extra HTTP requests needed.

India filtering is server-side via the `location` query parameter.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://careers.spglobal.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/jobs",
}

# description cache: apply_url → (description, posting_date)
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-05-07T00:00:00+0000' → '2026-05-07'."""
    return raw[:10] if raw else ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    params = {
        "keywords": keyword,
        "location": location or "India",
        "limit": num,
        "offset": start,
    }

    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"S&P Global Careers search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    data = r.json()
    raw_jobs = data.get("jobs", [])

    jobs = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = j.get("title", "").strip()
        city = j.get("city", "") or ""
        country = (j.get("country") or j.get("country_code") or "").strip()

        # Keep only India jobs (server-side location filter should handle this,
        # but guard against stray non-India results)
        location_str = f"{city}, India" if city else "India"
        if country and "in" not in country.lower() and "india" not in country.lower():
            if city.lower() not in {"mumbai", "noida", "hyderabad", "gurugram", "gurgaon", "bengaluru", "pune"}:
                continue

        posted_raw = j.get("posted_date", "")
        posting_date = _parse_date(posted_raw)

        apply_url = j.get("apply_url", "") or f"{_BASE_URL}/jobs/{job_id}"

        description = _strip_html(j.get("description", ""))
        _desc_cache[apply_url] = (description, posting_date)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": apply_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) — served from cache populated by fetch_jobs()."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Fallback: live fetch (should rarely be needed)
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers={**_HEADERS, "Accept": "text/html"}, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on {application_url}")
            r.raise_for_status()
            text = _strip_html(r.text)
            result = (text, "")
            _desc_cache[application_url] = result
            return result
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    return "", ""
