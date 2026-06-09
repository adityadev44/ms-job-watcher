"""Fetches Amazon job listings from the amazon.jobs public JSON API."""

from __future__ import annotations

import html as html_mod
import re
import time
from typing import Any

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

    location is ignored — country is hardcoded to IN (India) in the request,
    consistent with how optum_fetcher handles location filtering.
    """
    params: dict[str, Any] = {
        "query": keyword,
        "country[]": "IN",
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
            job_path = job.get("job_path", "")
            app_url = f"{_BASE_URL}{job_path}"

            # Cache description from inline search data — avoids a detail fetch later.
            combined = " ".join(
                _strip_html(job.get(field, "") or "")
                for field in ("description", "basic_qualifications", "preferred_qualifications")
            ).strip()
            _desc_cache[app_url] = combined

            results.append({
                "id": str(job.get("id", "")),
                "title": job.get("title", ""),
                "location": job.get("normalized_location") or job.get("location", ""),
                "posting_date": job.get("posted_date", ""),
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
