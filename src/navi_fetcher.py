"""Fetches Navi (Navi Technologies / Navi Limited) job listings.

ATS discovery (live, 2026-09-08): navi.com/careers links to
``navi.com/careers/jobs``, whose own client-side JS calls a first-party
proxy endpoint — ``GET https://navi.com/api/careers/jobs`` — which itself
wraps a TurboHire tenant (``navi.turbohire.co``), the same ATS vendor
already onboarded for Flipkart/JSW elsewhere in this repo, but accessed
here through Navi's own domain rather than directly against
turbohire.co. Confirmed live: plain ``requests.get()`` with no auth/
cookies needed, no Cloudflare/bot gating.

Response shape:
    GET /api/careers/jobs
      -> {"Total": N, "FilteredCount": N, "FiltersData": {...},
          "Jobs": [{"JobId", "JobTitle", "JobDescriptionV2" (HTML,
              INLINE), "Location": [{"Address": "City, State, India"}],
              "PublishedDate", "UpdatedDate", "ApplyUrl", ...}]}

Server-side filtering — tested live: a ``?search=`` query param exists
in principle, but both a real keyword ("engineer") and a nonsense token
return the identical ``FilteredCount: 36`` as no query at all — the
whole (small) board is returned regardless. Whole pool cached once per
process, same pattern as other small-board companies in this repo
(Vedanta/Perfios/etc.); matcher.py's own title/skill filters do the
real narrowing.

Current live board (2026-09-08): 36 total postings, all Bangalore/
Bengaluru or Delhi, India — no Pune/Chennai/Tamil Nadu/Kochi/Chandigarh
postings observed. Titles are mostly business/ops/finance roles, with a
couple of genuine engineering titles ("SDE III - Mobile Security",
"SDE III - Security") that will flow through the shared title-family/
skill matcher like any other company; 0 matches today is a legitimate
result if their descriptions don't happen to mention a qualifying
.NET/AI skill term, not a fetcher bug.

Location: ``Location`` is a JSON-encoded list of ``{"Address": "..."}``
dicts (already a string in the raw response — decoded here); the
Address string is already a clean "City, State, India" (or "City,
India") form, no normalization needed.

Description: ``JobDescriptionV2`` is inline HTML in the list response
already (no separate detail call needed) — stripped to plain text here.

Application URL: ``ApplyUrl`` is provided directly per job
(``https://navi.turbohire.co/job/publicjobs/{id}?utm_source=CareerPage``)
— used verbatim; job id parsed back out of it for description lookups
since the cache is keyed by job id, not URL.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime, timezone

import requests

_JOBS_API = "https://navi.com/api/careers/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Module-level cache: keyword is ignored server-side (verified live), so
# the whole (small) board is fetched and cached once per process.
_job_cache: list[dict] = []
_detail_cache: dict[str, dict] = {}
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from Navi's careers API."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _iso_to_date(raw) -> str:
    if not raw:
        return ""
    try:
        cleaned = raw.rstrip("Z")
        # Truncate fractional seconds beyond microsecond precision, if any.
        if "." in cleaned:
            head, frac = cleaned.split(".", 1)
            cleaned = f"{head}.{frac[:6]}"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _location_from_job(job: dict) -> str:
    raw_loc = job.get("Location")
    if isinstance(raw_loc, str):
        try:
            raw_loc = json.loads(raw_loc)
        except (ValueError, TypeError):
            raw_loc = []
    if isinstance(raw_loc, list) and raw_loc:
        addr = (raw_loc[0] or {}).get("Address") or ""
        if addr.strip():
            return addr.strip()
    return "India"


def _get_json(url: str, *, params: dict | None = None, timeout: int = 20, context: str = "") -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Navi {context}: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Navi {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Navi {context}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    try:
        data = _get_json(_JOBS_API, timeout=timeout, context="job list")
    except RateLimitError as exc:
        _cache_error = exc
        raise

    raw_jobs = (data or {}).get("Jobs") or []
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("JobId") or "").strip()
        title = (j.get("JobTitle") or "").strip()
        if not (job_id and title):
            continue
        apply_url = (j.get("ApplyUrl") or "").strip() or f"https://navi.turbohire.co/job/publicjobs/{job_id}"
        collected.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(j),
            "posting_date": _iso_to_date(j.get("PublishedDate") or j.get("UpdatedDate") or j.get("CreatedDate")),
            "application_url": apply_url,
        })
        _detail_cache[job_id] = {
            "description": _strip_html(j.get("JobDescriptionV2") or j.get("JobDescription") or ""),
            "posting_date": _iso_to_date(j.get("PublishedDate") or j.get("UpdatedDate") or j.get("CreatedDate")),
        }

    _job_cache[:] = collected
    print(f"[Navi] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Navi postings.

    keyword/location are accepted for interface compatibility but ignored
    server-side (verified live: a nonsense keyword returns the same
    ``FilteredCount`` as no keyword); the shared matcher does the real
    title/skill/India filtering. The whole (small) board is fetched and
    cached once per process.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    match = re.search(r"/publicjobs/([0-9a-fA-F-]+)", application_url or "")
    if match:
        return match.group(1)
    return (application_url or "").rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Navi job.

    Descriptions are already inline in the list response and cached by
    job id during ``_fill_cache`` — no separate detail call is needed.
    """
    _fill_cache(timeout=timeout)
    job_id = _job_id_from_url(application_url)
    detail = _detail_cache.get(job_id, {})
    return detail.get("description", ""), detail.get("posting_date", "")
