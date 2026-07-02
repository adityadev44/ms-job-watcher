"""Fetches MSCI job listings via Algolia (the search index that powers
careers.msci.com/job-search directly -- the site is an Algolia InstantSearch
frontend, not iCIMS's own API despite iCIMS subdomains existing for MSCI).

Discovered via Playwright network capture: the page posts to
https://{app_id}-dsn.algolia.net/1/indexes/*/queries with an
X-Algolia-Application-Id / X-Algolia-API-Key header pair. The API key is
Algolia's public "search-only" key type (intentionally exposed client-side,
safe to reuse -- this is how every Algolia-powered site's frontend works).

MSCI's total job count is small (~90 roles globally), so the whole India
subset is fetched in a single unfiltered query (empty search string, one
page covers everything) and cached in-module; title/skill filters do the
real narrowing. The `country` field on each hit is an exact "India" string
(no normalisation needed). Full plain-text description is already embedded
in each search hit -- no separate detail fetch required, same pattern as
S&P Global Careers/Gallagher/Cognizant.
"""
from __future__ import annotations

import time
import warnings
from datetime import datetime

import requests

_ALGOLIA_APP_ID = "RVMOB42DFH"
_ALGOLIA_API_KEY = "629e647c6a9a8b542fb1022001313a7e"
_ALGOLIA_URL = f"https://{_ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
_INDEX_NAME = "production__mscicare2201__sort-rank"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
    "X-Algolia-API-Key": _ALGOLIA_API_KEY,
    "Content-Type": "application/json",
    "Referer": "https://careers.msci.com/",
}

_cache: dict[str, dict] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from MSCI's Algolia index."""


def _parse_date(raw) -> str:
    """Algolia stores dates as unix-ms timestamps in some fields; best-effort."""
    if not raw:
        return ""
    try:
        ts = int(raw)
        if ts > 10_000_000_000:  # milliseconds
            ts //= 1000
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    _cache_filled = True  # set before try -- avoid retry storms on failure

    body = {"requests": [{"indexName": _INDEX_NAME, "params": "query=&hitsPerPage=200&page=0"}]}
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(_ALGOLIA_URL, headers=_HEADERS, json=body, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("MSCI: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"MSCI cache fill failed: {exc}") from exc

    results = r.json().get("results", [{}])
    hits = results[0].get("hits", []) if results else []
    for h in hits:
        oid = h.get("objectID", "")
        if oid and h.get("country") == "India":
            _cache[oid] = h


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return MSCI India job listings (fetched once, cached in-module)."""
    if start > 0:
        return []

    if not _cache_filled:
        _fill_cache(timeout)

    jobs: list[dict] = []
    for oid, h in _cache.items():
        title = (h.get("title") or "").strip()
        if not title:
            continue
        loc = h.get("town_city_country") or h.get("display_location") or "India"
        jobs.append({
            "id": oid,
            "title": title,
            "location": loc if "india" in loc.lower() else f"{loc}, India",
            "posting_date": "",
            "application_url": h.get("apply_url") or h.get("jd_url") or "",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) from the in-module cache -- no HTTP call.

    The Algolia hit already carries the full description; find it by
    matching application_url against the cached apply_url/jd_url.
    """
    for h in _cache.values():
        if application_url in (h.get("apply_url"), h.get("jd_url")):
            return (h.get("description") or "").strip(), ""
    return "", ""
