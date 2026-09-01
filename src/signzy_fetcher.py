"""Fetches Signzy job listings — Keka ATS embedded-jobs widget.

ATS discovery (2026-08-31): www.signzy.com/careers/ preloads a script from
`signzy.keka.com/careers/api/embedjobs/js/{widget_id}` — Keka, a new ATS
vendor for this repo (per PLAYBOOK's "Keka / Freshteam / custom React SPA"
identification note). Reading that embed script revealed the underlying
JSON API it calls (`khConfig.domain + api/embedjobs/${portalName}/active/
+ khConfig.identifier`), which resolves to:

    GET https://signzy.keka.com/careers/api/embedjobs/default/active/
        54e30b3d-e138-4862-8055-8b2ce8c31009
        -> [{id, title, description (full HTML), excerpt, departmentName,
             jobLocations: [{city, state, countryCode, countryName}],
             jobType, experience, jobNumber, publishedOn, skillNames, ...}]

No auth, no session cookie, plain `requests.get()` confirmed live — the
whole board (29 postings) comes back in one call, full HTML description
inline, no separate detail fetch needed. No keyword/pagination params
exist on this endpoint; matcher.py does the real filtering client-side.

Location: `jobLocations` is usually a one-item list with clean
`city`/`countryName` fields ("Bangalore"/"India") — already unambiguous,
no substring guessing needed. A handful of postings (e.g. "SRE-1",
"Mobile Application Developer-2") carry an empty `jobLocations: []` — a
genuine data gap on Signzy's side, not a parsing bug (confirmed by
inspecting the raw API response directly). Since every job that DOES carry
a location on this board is India-based and this portal has no non-India
presence, these are defaulted to "India" rather than dropped.

Application URL: `https://signzy.keka.com/careers/jobdetails/{id}`
(constructed from the embed script's own link-building logic —
`khConfig.domain + portalNameUrl + 'jobdetails/' + job.id` — and confirmed
live, HTTP 200).

Real engineering titles observed live: "SDE - 1 Fullstack Engineer",
"QA-1", "SRE-1"/"SRE Intern", "Mobile Application Developer-2" — small
board, most postings are Sales/Finance/CEO-office roles typical of an
early-stage fintech, so expect low match volume most cycles (same
"small-but-real" precedent as Zeta/Juspay elsewhere in this wave).
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_WIDGET_ID = "54e30b3d-e138-4862-8055-8b2ce8c31009"
_JOBS_URL = f"https://signzy.keka.com/careers/api/embedjobs/default/active/{_WIDGET_ID}"
_JOB_PAGE_BASE = "https://signzy.keka.com/careers/jobdetails/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.signzy.com/careers/",
}

# Module-level cache: this endpoint has no query params and always returns
# the full board, so fetch once per process.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _location_from_job(j: dict) -> str:
    locs = j.get("jobLocations") or []
    if not locs:
        return "India"
    parts = []
    for loc in locs:
        city = (loc.get("city") or loc.get("name") or "").strip()
        country = (loc.get("countryName") or "").strip()
        if city and country and country.lower() not in city.lower():
            parts.append(f"{city}, {country}")
        elif city:
            parts.append(city)
        elif country:
            parts.append(country)
    return "; ".join(parts) if parts else "India"


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Signzy (Keka) board once and cache it.

    _cache_filled is set to True before the fetch attempt so a failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() /
    fetch_job_description() call within the same process (Honeywell
    lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_JOBS_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Signzy: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Signzy cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Signzy cache fill: no response — {last_exc}")

    try:
        raw_jobs = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Signzy cache fill: invalid JSON — {exc}") from exc

    if not isinstance(raw_jobs, list):
        return

    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "").strip()
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        posted_on = (j.get("publishedOn") or "")[:10]

        collected.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(j),
            "posting_date": posted_on,
            "application_url": f"{_JOB_PAGE_BASE}{job_id}",
        })
        _desc_cache[job_id] = _strip_html(j.get("description") or j.get("excerpt") or "")

    _job_cache[:] = collected
    print(f"[Signzy] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Signzy jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    this endpoint has no query params and always returns the full board.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Signzy job.

    Served entirely from the cache filled by fetch_jobs() — the search
    response already includes the full description, no detail HTTP call.
    """
    _fill_cache(timeout=timeout)
    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
