"""Fetches Paytm job listings via the Lever ATS.

jobs.paytm.com is a thin frontend over Lever's public postings API
(`api.lever.co/v0/postings/paytm?mode=json`) — the `paytm` token works
directly, no iframe/script scraping needed to discover it.

Key quirks:
- Lever ignores keyword/location query params server-side for this tenant
  (there is no `?query=` support in the public postings API this pipeline
  uses) — the entire ~230-posting pool is fetched once and cached
  in-module, then sliced per `fetch_jobs()` call, same pattern as
  Persistent/Deutsche Bank/Bank of America.
- `descriptionPlain` is already the full plain-text job description in the
  search response — no per-job detail call needed. Registry should mark
  this fetcher inline (`_INLINE_DESCRIPTIONS`), like amazon/cognizant/
  gallagher (see report; company_registry.py itself is NOT edited here).
- Location handling: Lever's `categories.location` for Indian postings is
  a bare "City, State" string (e.g. "Noida, Uttar Pradesh", "Bangalore,
  Karnataka") that never contains the word "India" — matcher.py's
  `is_india_job()` (substring check for "india") would silently drop every
  genuine India posting without help. Unlike Lowe's/Invesco (which lack any
  authoritative country signal and must whitelist city names), Paytm's raw
  posting JSON carries a clean top-level `country` field (ISO alpha-2:
  "IN" for India, "LU"/"AE"/"CA"/"ID" for Luxembourg/Dubai/Toronto/Jakarta
  postings actually observed) — so this fetcher pre-filters on
  `country == "IN"` (authoritative, not a guess) and appends ", India" to
  the location string for jobs that pass, rather than leaving India
  detection to a city-name whitelist. Verified 2026-08-30: 218 of 228 total
  postings are `country == "IN"`; the 10 non-IN postings are Luxembourg (6,
  Paytm Europe entity), Dubai (2), Toronto (1), Jakarta (1).
"""
from __future__ import annotations

import html as html_mod
import time
from datetime import datetime, timezone

import requests

_API_URL = "https://api.lever.co/v0/postings/paytm?mode=json"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://jobs.paytm.com/",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure."""


# Module-level cache: filled once, reused for all keyword calls.
# _cache_filled is set to True BEFORE the fetch attempt (not after) so a
# failure doesn't trigger a retry storm on every subsequent fetch_jobs()
# call within the same process (Honeywell/Persistent lesson).
_india_cache: list[dict] = []
_desc_by_id: dict[str, str] = {}
_cache_filled: bool = False


def _epoch_ms_to_date(epoch_ms) -> str:
    if not epoch_ms:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _fill_cache(timeout: int = 20) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_API_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Paytm Lever: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Paytm cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Paytm cache fill: no response — {last_exc}")

    postings = r.json()
    collected: list[dict] = []
    for p in postings:
        if (p.get("country") or "").strip().upper() != "IN":
            continue

        job_id = p.get("id")
        title = (p.get("text") or "").strip()
        hosted_url = p.get("hostedUrl") or ""
        if not (job_id and title and hosted_url):
            continue

        loc = (p.get("categories", {}) or {}).get("location", "") or ""
        loc = loc.strip()
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

        # descriptionPlain has no HTML tags but does carry entity-escaped
        # punctuation ("&amp;", "&#39;") in ~11% of postings, and a handful
        # are double-escaped ("&amp;amp;", from source content that was
        # itself already entity-escaped before Lever re-escaped it) —
        # unescape twice to resolve both cases; unescaping plain text a
        # second time is a no-op so this is safe for already-clean strings.
        description = html_mod.unescape(html_mod.unescape(p.get("descriptionPlain") or ""))
        _desc_by_id[str(job_id)] = description

        collected.append({
            "id": str(job_id),
            "title": title,
            "location": loc,
            "posting_date": _epoch_ms_to_date(p.get("createdAt")),
            "application_url": hosted_url,
        })

    _india_cache = collected
    print(f"[Paytm] Cache filled: {len(collected)} India jobs (of {len(postings)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Paytm India jobs.

    Lever's public postings API ignores keyword/location for this tenant —
    the full pool is cached once and re-served (sliced) for every call.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single Paytm job.

    descriptionPlain is already the full plain-text description from the
    cached search response — no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]
    description = _desc_by_id.get(job_id, "")

    posting_date = ""
    for job in _india_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
