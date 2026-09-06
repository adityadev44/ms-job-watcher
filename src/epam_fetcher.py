"""Fetches EPAM Systems (careers.epam.com) India job listings via the
EPAM careers JSON REST API, discovered inside the Next.js bundle.

EPAM's careers site is a Next.js app backed by a content-delivery API served
from the same origin. The API endpoint is:

    GET /api/jobs/v2/search/{tenant}

with tenant "careers-india" returning EPAM India (Bengaluru, Hyderabad,
Pune, Kolkata, Chennai) jobs directly. This endpoint is public (no auth
required, confirmed 2026-09-06).

Key facts confirmed live 2026-09-06:
  - 318 India jobs accessible via tenant "careers-india".
  - Page size capped at 50 by the API (requesting 100 still returns 50).
  - Pagination: `from=N` (0-based offset), `size=50`.
  - Keyword filter: `q=<term>` is applied server-side with semantic/relevance
    matching — "xyzxyz9999nonsense" → 0, "python developer" → 221. The match
    is relevance-based rather than exact-substring, so per-keyword calls are
    issued and the shared matcher's title/skill checks do the final narrowing.
  - Each job has: `uid` (unique ID), `name` (title), `city[0].name` (city),
    `country[0].name` (always "India" for this tenant), `created_at` (ISO
    8601), `description` (full HTML JD), `seo.url` (slug for public URL).
  - Job URL: https://careers.epam.com/en{seo.url}
  - Descriptions are inline in the search response → coordinator should add
    "epam" to _INLINE_DESCRIPTIONS in the registry.

Pagination guard: `data.total` is the true total; stop when offset ≥ total
or when the returned `jobs` list is empty.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_API_BASE = "https://careers.epam.com"
_SEARCH_URL = f"{_API_BASE}/api/jobs/v2/search/careers-india"
_JOB_BASE_URL = f"{_API_BASE}/en"
_PAGE_SIZE = 50

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://careers.epam.com/en/jobs",
    "Origin": "https://careers.epam.com",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from EPAM's careers API."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _fetch_page(keyword: str, offset: int, timeout: int) -> dict:
    """GET one search page; 3-attempt exponential backoff."""
    params: dict[str, object] = {
        "lang": "en",
        "sortBy": "newest;relocation=asc",
        "size": _PAGE_SIZE,
        "from": offset,
        "websiteLocale": "en-us",
    }
    if keyword:
        params["q"] = keyword

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("EPAM: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"EPAM fetch failed: {exc}") from exc

    raise RateLimitError(f"EPAM: no response — {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of EPAM India jobs matching *keyword*.

    The "careers-india" tenant hard-filters to India; keyword is forwarded
    via the `q` param for server-side relevance ranking/filtering.
    Pagination maps start/num → API offset in _PAGE_SIZE chunks.
    """
    # Map start/num → which API page covers this slice
    first_api_page = start // _PAGE_SIZE
    last_api_page = (start + num - 1) // _PAGE_SIZE

    collected: list[dict] = []
    seen_ids: set[str] = set()
    total: int | None = None

    for page_idx in range(first_api_page, last_api_page + 1):
        offset = page_idx * _PAGE_SIZE
        raw = _fetch_page(keyword, offset, timeout)
        data = raw.get("data", {})
        if total is None:
            total = data.get("total", 0)

        items = data.get("jobs") or []
        if not items:
            break
        if total is not None and offset >= total:
            break

        for item in items:
            job_id = (item.get("uid") or "").strip()
            title = (item.get("name") or "").strip()
            if not (job_id and title):
                continue
            if job_id in seen_ids:
                continue

            # City: first entry in the city list
            cities = item.get("city") or []
            city = cities[0].get("name", "") if cities else ""
            location_str = f"{city}, India" if city and city.lower() != "india" else "India"

            # Date: "2026-09-03T15:00:15.531Z" → "2026-09-03"
            created = (item.get("created_at") or "")[:10]

            # Description is inline HTML
            desc_html = (item.get("description") or "")
            description = _strip_html(desc_html)

            # Job URL from SEO slug
            seo = item.get("seo") or {}
            seo_url = (seo.get("url") or "").strip()
            if seo_url:
                application_url = f"{_JOB_BASE_URL}{seo_url}"
            else:
                application_url = f"{_API_BASE}/en/jobs?q={job_id}"

            seen_ids.add(job_id)
            collected.append({
                "id": job_id,
                "title": title,
                "location": location_str,
                "posting_date": created,
                "description": description,
                "application_url": application_url,
            })

        if len(items) < _PAGE_SIZE:
            break  # partial page → end of results
        if page_idx < last_api_page:
            time.sleep(0.2)

    # Slice to the requested window within the fetched range
    offset_within_result = start - first_api_page * _PAGE_SIZE
    return collected[offset_within_result : offset_within_result + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """EPAM descriptions are returned inline by fetch_jobs."""
    raise NotImplementedError("descriptions are inline")
