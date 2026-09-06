"""
Postman job fetcher — Greenhouse public REST API.

Careers site: https://www.postman.com/company/careers/open-positions/
ATS confirmed live (2026-09-05): Greenhouse — all job links resolve to
https://job-boards.greenhouse.io/postman/jobs/<id>.

Search endpoint: GET https://boards-api.greenhouse.io/v1/boards/postman/jobs
  - No server-side keyword or location filter — the endpoint always returns
    the full global job pool regardless of query params. The full board
    (63 jobs as of 2026-09-05, 4 of which are India-based) is fetched once
    per process and cached; all India filtering happens client-side via the
    `location.name` field.
  - Adding `?content=true` returns each job's full HTML description inline,
    avoiding per-job detail fetches entirely.

Live-verified 2026-09-05:
- 63 total postings globally.
- 4 India postings: 3 Bengaluru Karnataka and 1 Hyderabad Telangana.
- All India roles are software-engineering: "Senior Engineer - Fabric Gateway",
  "Senior Engineer, IAM", "Staff Engineer – Observability Platform"
  (and 1 enterprise sales role).
- Keywords are NOT filtered server-side — the full pool is cached once and
  matcher.py does all title/skill matching.

India detection: the `location.name` field uses strings like
"Bengaluru, Karnataka, India" or "Hyderabad, Telangana, India" — the word
"india" is always present, so a simple case-insensitive substring check
on `location.name` is sufficient.

Descriptions: fully inline in the `content` field (HTML — `_strip_html`
applied). No per-job HTTP call needed; `fetch_job_description` is served
from the cache built during `fetch_jobs()` / `_fill_cache()`.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""

_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/postman/jobs"
_JOB_DETAIL_URL = "https://boards-api.greenhouse.io/v1/boards/postman/jobs/{job_id}"
_PUBLIC_BASE = "https://job-boards.greenhouse.io/postman/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.postman.com/company/careers/open-positions/",
}

# Module-level cache: the Greenhouse board is fetched once per process and
# reused for all keyword/page calls (same "cache-once + _cache_filled"
# pattern as Atlassian/Groww/ShareChat in this repo).
_india_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled: bool = False

# Pagination-wraparound guard
_FIRST_PAGE_IDS: set[str] | None = None


class _NotRateLimit(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-08-31T12:17:49-04:00' -> '2026-08-31'."""
    return raw[:10] if raw else ""


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Postman Greenhouse board once and cache India jobs.

    `_cache_filled` is set True *before* the network call so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()
    call within the same process (Honeywell/Persistent lesson).
    """
    global _cache_filled, _india_cache
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _JOBS_URL,
                params={"per_page": 500, "content": "true"},
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Postman: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Postman cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Postman cache fill: no response — {last_exc}")

    try:
        raw_jobs = r.json().get("jobs", [])
    except ValueError as exc:
        raise RateLimitError(f"Postman cache fill: invalid JSON — {exc}") from exc

    total = 0
    collected: list[dict] = []
    for j in raw_jobs:
        total += 1
        loc_name = (j.get("location") or {}).get("name") or ""
        if "india" not in loc_name.lower():
            continue

        job_id = str(j.get("id") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        if not title:
            continue

        posting_date = _parse_date(j.get("updated_at") or j.get("first_published") or "")
        apply_url = j.get("absolute_url") or f"{_PUBLIC_BASE}/{job_id}"
        description = _strip_html(j.get("content") or "")

        _desc_cache[apply_url] = (description, posting_date)

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_name,
            "posting_date": posting_date,
            "application_url": apply_url,
        })

    _india_cache = collected
    print(f"[Postman] Cache filled: {len(collected)} India jobs (of {total} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Postman India jobs from the cached board.

    keyword/location are accepted for interface compatibility but ignored
    server-side — Greenhouse returns the full board regardless of query
    parameters. All keyword/title matching is handled by matcher.py after
    this call returns.
    """
    global _FIRST_PAGE_IDS

    _fill_cache(timeout=timeout)
    page = _india_cache[start: start + num]

    # Pagination-wraparound guard
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in page}
    elif _FIRST_PAGE_IDS and {j["id"] for j in page} == _FIRST_PAGE_IDS:
        return []

    return page


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) from the cache built by fetch_jobs().

    Descriptions are inline in the Greenhouse `content=true` list response;
    no separate HTTP call is ever needed.
    """
    _fill_cache(timeout=timeout)
    if application_url in _desc_cache:
        return _desc_cache[application_url]
    return "", ""
