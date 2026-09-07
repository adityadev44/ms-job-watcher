"""Fetches MongoDB job listings via the Greenhouse ATS.

MongoDB's careers page (mongodb.com/careers) is Greenhouse-backed, board
token "mongodb". Confirmed live 2026-09-06:

    GET https://boards-api.greenhouse.io/v1/boards/mongodb/jobs?content=true

-- HTTP 200, 415 total postings globally.

Same "cache-once, ignores keywords" family as every other Greenhouse board in
this repo (Glean/Groww/Razorpay/AlphaSense): Greenhouse's public job-list
endpoint has no keyword/location query param at all (verified: appending
`&q=zzznonsensequeryabc123` returns the identical 415-job payload), returns
the ENTIRE current board in one call with no server-side pagination, so the
whole board is fetched once, cached in-module, and India jobs are filtered
out during the cache fill. `_cache_filled` is set to True *before* the fetch
attempt (Honeywell/Persistent lesson re: retry storms).

Location quirk (bare city names, no "india" substring -- same family as
Zeta/CRED/Lowe's/AB InBev): MongoDB's `location.name` field is a bare city
name for every India posting observed -- "Gurugram" (its largest India
office by far) or "Bengaluru", occasionally "Bengaluru; Gurugram" (multi-
office reqs), and a couple of literal "India" (remote-India roles). None of
these contain the substring "india" except the literal-"India" ones, so
matcher.py's substring-based `is_india_job()` would silently drop every
Gurugram/Bengaluru posting if passed through unmodified. Normalized here via
a small recognized-city whitelist (Lowe's/AB InBev convention): only
"gurugram"/"bengaluru"/"bangalore" get ", India" appended; anything else is
left untouched (fails `is_india_job()` safely rather than being guessed at).

Verified live 2026-09-06: 65 India-located postings (of 415 global), heavily
Gurugram-based (its GTM-tech/business-systems engineering hub) with a real
Bengaluru solutions-architecture/support presence. Strong genuine-engineering
title signal: "Senior Software Engineer, AI Builder Experience (ABX)",
multiple "Software Engineer 3" reqs, "Staff Engineer", "Senior Platform
Engineer", "Senior Site Reliability Engineer", "Engineering Manager, AI
Builder Experience", "Senior/Staff Forward Deployed Engineer", "Lead,
Engineering" -- these map straight onto the "software engineer"/"senior
software engineer"/"ai engineer" keyword set.

Key quirks (same double-encoding family already documented for Glean/Groww/
Razorpay/AlphaSense):
- `content` (full HTML JD) may be double HTML-entity-encoded; `_strip_html`
  unescapes twice before stripping tags, matching glean_fetcher.py's
  empirically-verified approach.
- `first_published` (genuine original post date) preferred over `updated_at`
  (bumped on any board-wide re-index -- every single job above shares the
  identical `updated_at` of "2026-09-01T03:40:27" regardless of actual
  posting age, confirming `updated_at` is NOT a reliable per-job signal here).
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "mongodb"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL_TMPL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.mongodb.com/careers",
}

# Bare city names observed on this tenant's India postings -- no "india"
# substring naturally present (Lowe's/AB InBev whitelist convention).
_INDIA_CITIES = {"gurugram", "bengaluru", "bangalore"}

_india_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Greenhouse."""


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(_html_mod.unescape(raw or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_date(job: dict) -> str:
    raw = job.get("first_published") or job.get("updated_at") or ""
    return raw[:10] if raw else ""


def _normalize_location(raw_loc: str) -> str:
    """Append ', India' only for recognized bare India city names."""
    loc = (raw_loc or "").strip()
    if not loc:
        return loc
    if "india" in loc.lower():
        return loc
    parts = [p.strip() for p in loc.split(";") if p.strip()]
    if any(p.lower() in _INDIA_CITIES for p in parts):
        return f"{loc}, India"
    return loc


def _get_with_retry(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"MongoDB {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"MongoDB {what} failed: {exc}") from exc
    raise RateLimitError(f"MongoDB {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = _get_with_retry(f"{_LIST_URL}?content=true", timeout, "cache fill")
    raw_jobs = r.json().get("jobs", [])

    collected: list[dict] = []
    for job in raw_jobs:
        job_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc_name = ((job.get("location") or {}).get("name") or "").strip()
        loc_norm = _normalize_location(loc_name)
        if "india" not in loc_norm.lower():
            continue

        app_url = job.get("absolute_url") or f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs/{job_id}"

        _content_cache[job_id] = job.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_norm,
            "posting_date": _parse_date(job),
            "application_url": app_url,
        })

    _india_cache[:] = collected
    print(f"[MongoDB] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of MongoDB India jobs.

    Greenhouse's public job-list endpoint has no keyword/location query
    param and always returns the full current board, so the pool is
    fetched once and cached; keyword/location arguments are accepted for
    interface compatibility but not applied here (the shared title/skill
    filters in matcher.py do the real narrowing).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start: start + num]


def _job_id_from_url(application_url: str) -> str:
    """Extract the Greenhouse job id from a MongoDB career-site URL.

    MongoDB's own `absolute_url` has NO '/jobs/{id}' path segment (unlike
    most other Greenhouse-backed career sites in this repo, e.g. Cloudflare/
    GitLab) -- it is a bare 'https://www.mongodb.com/careers/job/?gh_jid=
    {id}' query-string format. Both shapes are checked defensively in case
    a future application_url ever carries the id in the path instead.
    """
    url = application_url or ""
    m = re.search(r"/jobs/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]gh_jid=(\d+)", url)
    return m.group(1) if m else ""


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single MongoDB job."""
    job_id = _job_id_from_url(application_url)

    if job_id and job_id in _content_cache:
        return _strip_html(_content_cache[job_id]), ""

    if not job_id:
        raise RateLimitError(f"MongoDB description: could not parse job id from {application_url!r}")

    r = _get_with_retry(_DETAIL_URL_TMPL.format(job_id=job_id) + "?content=true", timeout, "description fetch")
    job = r.json()
    description = _strip_html(job.get("content") or "")
    posting_date = _parse_date(job)
    _content_cache[job_id] = job.get("content") or ""
    return description, posting_date
