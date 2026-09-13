"""
Agoda (Booking Holdings) job fetcher — Greenhouse ATS REST API.

Agoda's branded careers domain (careersatagoda.com) is behind a Cloudflare
managed challenge and returns HTTP 403 "Just a moment..." to plain requests
even with a browser User-Agent — same shape as the Dover/Payoneer branded-
domain lesson in the playbook. The real ATS is a public Greenhouse board
(`job-boards.greenhouse.io/agoda`, confirmed via web search and a direct
hit against the public boards API), which is fully open to plain HTTP with
no challenge at all:

GET https://boards-api.greenhouse.io/v1/boards/agoda/jobs?content=true

returns the entire board (~290 postings) in one call, including each job's
full HTML `content` field — no per-job detail fetch needed, and no
Playwright required anywhere in this fetcher. Like other Greenhouse-style
"ignores query params" ATSes in this repo (Groww/Razorpay), keyword and
location are not applied server-side, so the full pool is fetched once and
cached in-module (`_cache_filled` is set *before* the fetch attempt — the
Honeywell lesson — so a transient failure doesn't retry-storm on every
subsequent call in the same process).

There is no separate posting-date field; `updated_at` (truncated to
YYYY-MM-DD) is used as `posting_date`, per Greenhouse's standard shape.

Location quirk: Agoda's India office postings show up in a few different
raw shapes — "Gurgaon, India" and bare "India" already contain the literal
word, but some say "Gurugram, IND | Gurugram" (abbreviated country code,
no "india" substring) which would be silently invisible to matcher.py's
is_india_job(). Following the Razorpay/Lowe's/Invesco convention: all jobs
are still returned un-filtered from fetch_jobs (this fetcher does not
decide India vs. non-India), but ", India" is appended to recognised India
city names only when the string doesn't already say "India" — never
blindly — so combined multi-country postings like "Gurgaon, Bangkok, Pune
& Kuala Lumpur" are left alone and correctly excluded downstream (no
"india" substring at all, so is_india_job() rejects them, consistent with
the SimCorp/Maersk "ambiguous multi-site" caution — these roles are not
confirmed India-primary).

Content quirk: this board's `content` field is HTML-entity-escaped one
extra level compared to a typical Greenhouse board (same as Razorpay) —
the raw string is literally "&lt;div class=&quot;...&quot;&gt;..." rather
than "<div class=\"...\">...". `_strip_html` unescapes once before
stripping tags and unescapes again afterward for any inline entities in
the actual text.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_TOKEN = "agoda"
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

# Indian city tokens seen on this board — office locations are sometimes
# city-only or use the abbreviated "IND" country code instead of "India".
# Used to normalise recognised India cities to include ", India" without
# blindly trusting every location string.
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
    """Fetch the full Agoda Greenhouse board once and cache it.

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
                raise RateLimitError("Agoda: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Agoda cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Agoda cache fill: no response — {last_exc}")

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
    print(f"[Agoda] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Agoda jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full board regardless
    of query params. All jobs (India and non-India) are returned — India
    scoping is left to matcher.py's is_india_job() / config exclude_locations,
    aside from the city-name normalisation documented in the module docstring.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Agoda job.

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
