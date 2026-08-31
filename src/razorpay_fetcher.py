"""
Razorpay job fetcher — Greenhouse ATS REST API.

Board token: razorpaysoftwareprivatelimited. Public boards API:
GET https://boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs?content=true
returns the entire board (~21 postings) in one call, including each job's
full HTML `content` field — no per-job detail fetch needed. Like other
Greenhouse-style "ignores query params" ATSes in this repo, keyword and
location are not applied server-side, so the full pool is fetched once
and cached in-module (`_cache_filled` is set *before* the fetch attempt —
the Honeywell lesson — so a transient failure doesn't retry-storm on
every subsequent call in the same process).

There is no separate posting-date field; `updated_at` (truncated to
YYYY-MM-DD) is used as `posting_date`, per Greenhouse's standard shape.

Location quirk: `location.name` is a bare city name with no country word
("Bengaluru", "Coimbatore", "Singapore", "Malaysia") — matcher.py's
is_india_job() requires a literal "india" substring, so returning city
names as-is would silently drop every genuine India posting. Following
the Lowe's/Invesco convention (see PLAYBOOK "Key Bugs"): all jobs are
still returned un-filtered from fetch_jobs (this fetcher does not decide
India vs. non-India), but ", India" is appended to recognised India city
names only — never blindly — so Singapore/Malaysia postings are left
alone and correctly rejected downstream by is_india_job() /
exclude_locations.

Content quirk: this board's `content` field is HTML-entity-escaped one
extra level compared to a typical Greenhouse board — the raw string is
literally "&lt;div class=&quot;...&quot;&gt;..." rather than
"<div class=\"...\">...". `_strip_html` unescapes once before stripping
tags (turning entities into real "<...>" markup so the tag regex can
match it) and unescapes again afterward for any inline entities in the
actual text (e.g. "&amp;", "&nbsp;") — this order is a no-op cost on a
normally-escaped board, so it's safe either way.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_TOKEN = "razorpaysoftwareprivatelimited"
_BOARDS_BASE = "https://boards-api.greenhouse.io/v1/boards"
_JOBS_URL = f"{_BOARDS_BASE}/{_BOARD_TOKEN}/jobs"
_FALLBACK_JOB_BASE = f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Indian city tokens seen (or plausible) on this board — office locations
# are city-only, no country word. Used to normalise recognised India
# cities to include ", India" without blindly trusting every location.
_INDIA_CITIES = (
    "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "chennai",
    "gurugram", "gurgaon", "noida", "delhi", "coimbatore", "ahmedabad",
    "kolkata", "jaipur", "chandigarh", "kochi", "trivandrum",
)

# Module-level cache: the full board is fetched once and reused for every
# keyword/page call (Greenhouse's public boards API ignores query params).
_job_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _is_india_city(loc: str) -> bool:
    low = loc.lower()
    return any(city in low for city in _INDIA_CITIES)


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Razorpay Greenhouse board once and cache it.

    _cache_filled is set to True before the fetch attempt so a failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() /
    fetch_job_description() call within the same process (Honeywell
    lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _JOBS_URL,
                headers=_HEADERS,
                params={"content": "true"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Razorpay: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Razorpay cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Razorpay cache fill: no response — {last_exc}")

    raw_jobs = r.json().get("jobs", [])
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc = ((j.get("location") or {}).get("name") or "").strip()
        if loc and _is_india_city(loc) and "india" not in loc.lower():
            loc = f"{loc}, India"

        updated_at = j.get("updated_at") or ""
        posting_date = updated_at[:10] if updated_at else ""

        app_url = j.get("absolute_url") or f"{_FALLBACK_JOB_BASE}/{job_id}"

        _content_cache[job_id] = j.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache = collected
    print(f"[Razorpay] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Razorpay jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full board regardless
    of query params. All jobs (India and non-India) are returned — India
    scoping is left to matcher.py's is_india_job() / config exclude_locations,
    aside from the city-name normalisation documented in the module docstring.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Razorpay job.

    Served entirely from the cache filled by _fill_cache() — Greenhouse's
    search response already includes the full HTML `content` field for
    every job, so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").split("/")[-1]
    description = _strip_html(_content_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
