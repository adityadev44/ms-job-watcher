"""
Sigmoid (Sigmoid Analytics) job fetcher — Greenhouse ATS REST API.

Sigmoid is a data engineering / analytics / AI consultancy with a large
Bengaluru delivery center. Its careers page (`www.sigmoid.com/careers`,
reached via a redirect from the older `sigmoidanalytics.com/careers`
domain) links to `www.sigmoid.com/careers/current-openings/`, which embeds
absolute `https://job-boards.greenhouse.io/sigmoid/jobs/{id}` links
directly in the server-rendered HTML — confirmed live 2026-09-13. Board
token: sigmoid. Public boards API:

    GET https://boards-api.greenhouse.io/v1/boards/sigmoid/jobs?content=true

Confirmed live: HTTP 200, 35 total postings, each with a full HTML
`content` field already inline (no per-job detail fetch needed) — same
"cache-once" pattern as every other Greenhouse board in this repo
(AlphaSense/Groww/Razorpay/Paytm). Query params are not applied
server-side, so the full pool is fetched once and cached in-module;
`_cache_filled` is set *before* the fetch attempt (the Honeywell lesson)
so a transient failure doesn't retry-storm on every subsequent call in the
same process.

There is no separate posting-date field; `updated_at` (truncated to
YYYY-MM-DD) is used as `posting_date`, per Greenhouse's standard shape.

Location quirk: most India postings already carry the full
"City, State, India" shape (e.g. "Bengaluru, Karnataka, India",
"Hyderabad, Andhra Pradesh, India"), but a handful of postings (HR/intern
roles, one DevOps role) use a bare city name with no country word at all
("Bangalore", "Bengaluru"). Following the AlphaSense/Razorpay convention:
all jobs are returned un-filtered from fetch_jobs (this fetcher does not
decide India vs. non-India), but ", India" is appended to recognised
India city names that are missing the country word — never blindly — so
non-India locations (Atlanta, New York, United Kingdom, etc., all present
on this board) are left alone and correctly rejected downstream by
is_india_job(). No Chennai/Pune/Kochi/Chandigarh/Tamil Nadu postings were
observed live; exclude_locations still applies as a safety net.

Title signal: engineering titles on this board are specific and
distinct ("Full Stack Software Development Engineer II", "Software
Development Engineer II", "Technical Lead", "Associate Lead Data
Scientist", "Director Data Science") rather than generic IT-services
level bands, and a live-fetched "Full Stack Software Development Engineer
II" (Bengaluru) explicitly names React/TypeScript/Node.js/Python
(FastAPI)/PostgreSQL in its body — real, verifiable AI/ML/Python-track
signal. `require_tech_in_description` is NOT enabled.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_TOKEN = "sigmoid"
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

_INDIA_CITIES = (
    "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "chennai",
    "gurugram", "gurgaon", "noida", "delhi", "new delhi", "coimbatore",
    "ahmedabad", "kolkata", "jaipur", "chandigarh", "kochi", "trivandrum",
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
    """Fetch the full Sigmoid Greenhouse board once and cache it."""
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
                raise RateLimitError("Sigmoid: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Sigmoid cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Sigmoid cache fill: no response — {last_exc}")

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
    print(f"[Sigmoid] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Sigmoid jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full board regardless
    of query params.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Sigmoid job.

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
