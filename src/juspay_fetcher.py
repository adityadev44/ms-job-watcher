"""Fetches Juspay job listings via its own custom career-site JSON API.

ATS discovery (2026-08-31): juspay.in/careers 301-redirects to
juspay.io/careers (an Astro-built site). No third-party ATS vendor —
grepping the page's own Content-Security-Policy header revealed the real
data source directly: `connect-src` allow-lists
`https://joinus.juspay.in/api/careerJobOpening`, a plain unauthenticated
JSON endpoint:

    GET https://joinus.juspay.in/api/careerJobOpening
        -> {"allJobs": [{job_id, job_title, job_location, category,
                          job_type, experience_year, is_global,
                          job_description_career (markdown), ...}]}

No pagination, no keyword/location params accepted or needed — the entire
current board (confirmed live: 10 open postings) comes back in one call.
`job_description_career` is already Markdown/plain text (not HTML) — no
tag-stripping needed, just whitespace normalization.

Application URL: the site itself has no `<a href>` per job card in the
static HTML (job cards are client-rendered React/Astro islands with a
JS-driven route), so the real per-job URL was found by driving headless
Firefox to https://juspay.io/careers and clicking a job card, then reading
`page.url` / `<a href>` targets: `https://juspay.io/careers/{job_id}`
(verified: HTTP 200, renders the correct job + Apply button). Plain
`requests` on that URL also returns 200 — no Playwright needed for the
detail page either, but no separate detail fetch is even necessary since
the search response already carries the full description inline.

Location: `job_location` is a bare city name ("Bangalore", "Mumbai",
"Dublin (Ireland)"). This board also carries a handful of genuinely
international roles (`is_global: true`, e.g. a Dublin customer-success
role) — left as-is; matcher.py's is_india_job() correctly drops anything
that doesn't say "India", so recognised India cities get ", India"
appended (Lowe's/Invesco/Razorpay convention) and everything else (Dublin,
etc.) is passed through unmodified and rejected downstream.

Small board, worth noting: only 10 postings live as of this check, 2 of
them genuine engineering roles (Software Development Engineer Frontend/
Backend, Bangalore) plus a Quality Engineer — expect low/zero match volume
most cycles, same "small-but-real" precedent as CRED/Juspay-sibling boards
elsewhere in this repo (Zeta, eClerx).
"""
from __future__ import annotations

import time

import requests

_API_URL = "https://joinus.juspay.in/api/careerJobOpening"
_JOB_PAGE_BASE = "https://juspay.io/careers/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://juspay.io/careers",
}

# Indian city tokens seen on this board — bare city names carry no "India"
# substring, so recognised ones get it appended; anything else (Dublin,
# São Paulo, Singapore, San Francisco) is left alone and correctly excluded
# downstream by is_india_job().
_INDIA_CITIES = (
    "bangalore", "bengaluru", "mumbai", "hyderabad", "pune", "chennai",
    "gurugram", "gurgaon", "noida", "delhi", "coimbatore", "ahmedabad",
    "kolkata", "jaipur", "chandigarh", "kochi", "trivandrum",
)

# Module-level cache: this endpoint returns the whole board regardless of
# query params (there are none), so fetch once per process.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _is_india_city(loc: str) -> bool:
    low = loc.lower()
    return any(city in low for city in _INDIA_CITIES)


def _normalize_location(loc: str) -> str:
    loc = (loc or "").strip()
    if loc and _is_india_city(loc) and "india" not in loc.lower():
        return f"{loc}, India"
    return loc or "India"


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Juspay board once and cache it.

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
            r = requests.get(_API_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Juspay: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Juspay cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Juspay cache fill: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Juspay cache fill: invalid JSON — {exc}") from exc

    raw_jobs = (data or {}).get("allJobs") or []
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("job_id") or "").strip()
        title = (j.get("job_title") or "").strip()
        if not (job_id and title):
            continue

        collected.append({
            "id": job_id,
            "title": title,
            "location": _normalize_location(j.get("job_location") or ""),
            "posting_date": "",  # not exposed by this API
            "application_url": f"{_JOB_PAGE_BASE}{job_id}",
        })
        _desc_cache[job_id] = " ".join((j.get("job_description_career") or "").split())

    _job_cache[:] = collected
    print(f"[Juspay] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Juspay jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    this endpoint has no query params and always returns the full board.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Juspay job.

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
