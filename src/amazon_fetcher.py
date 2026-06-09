"""Fetches Amazon job listings from the amazon.jobs public JSON API."""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests

_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
_BASE_URL = "https://www.amazon.jobs"
_PAGE_SIZE = 10  # confirmed working page size for amazon.jobs API

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Descriptions are returned inline in the search response — cached here
# by application_url so fetch_job_description can return them without an
# extra HTTP call.
_desc_cache: dict[str, str] = {}


class RateLimitError(Exception):
    """Raised when the API rate-limits after all retries are exhausted."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_posted_date(raw: str) -> str:
    """Normalise Amazon date strings to ISO YYYY-MM-DD.

    Amazon returns dates like 'June  9, 2026' (with occasional double-space
    before single-digit days). strptime accepts single or double-digit %d so
    normalising whitespace first is enough.
    """
    if not raw:
        return ""
    normalised = " ".join(raw.split())
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalised, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return one page of Amazon India job listings matching keyword.

    location is ignored — country is hardcoded to IND in the request,
    consistent with how optum_fetcher handles location filtering.
    Note: Amazon's API uses the 3-letter ISO code 'IND' and a plain
    'country' parameter (no brackets) for reliable country filtering.
    """
    params = {
        "query": keyword,
        "country": "IND",
        "offset": start,
        "result_limit": num,
        "sort": "recent",
    }
    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                _SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout
            )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Request failed after {_MAX_ATTEMPTS} attempts"
            ) from exc

        if response.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")

        response.raise_for_status()
        jobs_raw = response.json().get("jobs") or []

        results = []
        for job in jobs_raw:
            # Safety guard: skip any non-India result in case the filter slips.
            if job.get("country_code", "").upper() != "IND":
                continue

            job_path = job.get("job_path", "")
            app_url = f"{_BASE_URL}{job_path}"

            # Cache description from inline search data — avoids a detail fetch later.
            combined = " ".join(
                _strip_html(job.get(field, "") or "")
                for field in ("description", "basic_qualifications", "preferred_qualifications")
            ).strip()
            _desc_cache[app_url] = combined

            loc = job.get("normalized_location") or job.get("location", "")
            # Amazon location strings use codes like "IN, KA, Bengaluru"; append
            # ", India" so matcher's is_india_job() check ("india" in location) works.
            if "india" not in loc.lower():
                loc = f"{loc}, India".strip(", ")

            results.append({
                "id": str(job.get("id", "")),
                "title": job.get("title", ""),
                "location": loc,
                "posting_date": _parse_posted_date(job.get("posted_date", "")),
                "application_url": app_url,
            })
        return results

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Amazon job.

    Descriptions are cached during fetch_jobs so no additional HTTP call is
    needed. posting_date is already set from the search result.
    """
    return _desc_cache.get(application_url, ""), ""
