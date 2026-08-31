"""Fetches Meesho job listings via the public Lever ATS REST API.

careers.meesho.io is a Lever-hosted board (token "meesho"):
    GET https://api.lever.co/v0/postings/meesho?mode=json

Lever returns the entire posting board in one response and ignores any
keyword/location filtering — there is no server-side query support at all,
just this one list endpoint. So the full board is fetched once, cached
in-module, and fetch_jobs() slices the cache. Every posting's full plain-text
description (descriptionPlain) is already present in the same response, so
fetch_job_description() is served from that cache too (no per-job detail
call needed) — same "inline description" shape as amazon/cognizant/gallagher.

India filtering: Lever's categories.location string for this tenant is just
"City, State" (e.g. "Bangalore, Karnataka") with no "India" substring, which
would fail matcher.py's is_india_job() (checks for "india" in the location
text) even though every current Meesho posting genuinely is India-based. Lever
does provide a separate, reliable `country` field ("IN" for all postings
observed). Chose to pre-filter on `country == "IN"` here (structured field,
not a string-matching heuristic) AND append ", India" to the location string
before returning it, so is_india_job() also passes as a defense-in-depth
safety net rather than relying on it alone to do the real filtering.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

_BASE_URL = "https://api.lever.co/v0/postings/meesho"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_ID_RE = re.compile(r"/meesho/([0-9a-f-]{36})")

# Module-level cache: filled once, reused for all keyword/pagination calls.
_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent failure fetching the Lever board."""


def _parse_created_at(created_at) -> str:
    """Convert Lever's epoch-milliseconds createdAt to YYYY-MM-DD."""
    if not created_at:
        return ""
    try:
        return datetime.fromtimestamp(
            int(created_at) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Meesho Lever board once and cache India postings.

    _cache_filled is set to True *before* the fetch attempt (Honeywell
    lesson) so a failure here doesn't trigger a retry storm on every
    subsequent fetch_jobs() call in the same process.
    """
    global _cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _BASE_URL,
                headers=_HEADERS,
                params={"mode": "json"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Meesho Lever: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Meesho fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Meesho fetch: no response — {last_exc}")

    collected: list[dict] = []
    for p in r.json():
        job_id = p.get("id") or ""
        title = (p.get("text") or "").strip()
        hosted_url = p.get("hostedUrl") or ""
        if not (job_id and title and hosted_url):
            continue

        # Structured India filter — reliable, not a string heuristic.
        if (p.get("country") or "").strip().upper() != "IN":
            continue

        cat = p.get("categories") or {}
        raw_loc = (cat.get("location") or "").strip()
        # Lever's location text for this tenant has no "India" substring
        # (e.g. "Bangalore, Karnataka") — append it so matcher.py's
        # is_india_job() also passes as a safety net.
        location = f"{raw_loc}, India" if raw_loc else "India"

        description = p.get("descriptionPlain") or ""
        _desc_cache[job_id] = description

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": _parse_created_at(p.get("createdAt")),
            "application_url": hosted_url,
        })

    _cache = collected
    print(f"[Meesho] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Meesho India jobs.

    Lever ignores keyword/location server-side and always returns the full
    board, so both parameters are unused here — the shared title/skill
    filters in matcher.py do the real narrowing. The full India-filtered
    pool is fetched and cached once per process.
    """
    _fill_cache(timeout=timeout)
    return _cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for one job.

    descriptionPlain is already present in the cached search response, so
    this never makes a network call once the cache is filled — it only
    parses the job ID out of the hostedUrl/applyUrl and looks it up.
    """
    _fill_cache(timeout=timeout)

    m = _ID_RE.search(application_url or "")
    job_id = m.group(1) if m else ""

    description = _desc_cache.get(job_id, "")
    return description, ""
