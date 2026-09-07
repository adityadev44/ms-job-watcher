"""Fetches Cloudflare job listings via the Greenhouse ATS.

Cloudflare's careers page (cloudflare.com/careers) is Greenhouse-backed,
board token "cloudflare". Confirmed live 2026-09-06:

    GET https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs?content=true

-- HTTP 200, 331 total postings globally. Same "cache-once, ignores
keywords/location" family as every other Greenhouse board in this repo
(MongoDB/Glean/Groww/Razorpay/AlphaSense) -- the public job-list endpoint has
no keyword/location query param and always returns the identical full board
(verified: `&q=zzznonsensequeryabc123` returns the same 331 jobs), so the
whole board is cached once and India jobs are filtered during the fill.

**Critical location gotcha, specific to this tenant**: the top-level
`location.name` field is USELESS for India detection here -- Cloudflare
populates it with a work-mode label ("Hybrid", "In-Office", "Distributed",
"Remote"), not a place name, for the overwhelming majority of postings. Only
2 of 331 jobs have a place-like `location.name` ("Remote India"), and relying
on that field alone would silently miss every India posting that isn't fully
remote.

The real, authoritative location signal is a per-job `metadata` entry named
**"Job Posting Location"** (`value_type: "multi_select"`, a list of strings
like `["Bengaluru, India"]`) -- this is populated correctly and consistently
(verified against every India posting found) even when `location.name` says
"Hybrid"/"In-Office"/etc. This fetcher reads `metadata["Job Posting
Location"]` as the primary/only location source, falling back to
`location.name` only if that metadata key is absent (defensive, not
expected to trigger given the 100% coverage observed). Using
`metadata["Job Posting Location"]` instead of the generic substring-scan
across the whole location.name pool also avoids a real false positive: doing
a blind "does any field contain 'india'" scan would otherwise need to guard
against `location.name` values that never say "india" in the first place
(they say "Hybrid" etc.), so this is a strictly-more-correct signal, not just
a cosmetic preference.

Verified live 2026-09-06: 17 India-located postings (of 331 global) once
`metadata["Job Posting Location"]` is used -- vs. only 2 if `location.name`
alone were trusted. All 17 are Bengaluru-based. Genuine engineering-title
signal: "Software Engineer, AI Agents", "Software Engineer - Platforms &
Productivity", "Senior Data Engineer", "Security Engineer (IAM)", "Senior
Systems Engineer", "Systems Engineer (Data Intelligence & Analytics Team)",
"Incident Response Analyst - React", "Full Stack Engineer - Internal Audit"
-- a real, if modest, India engineering presence; the remaining ~9 are
sales/audit/procurement/accounting roles that still serve as useful breadth
for the shared title/skill matcher.

Key quirks (same double-encoding family already documented for Glean/
MongoDB/Groww/Razorpay/AlphaSense):
- `content` (full HTML JD) is unescaped twice before tag-stripping.
- `first_published` (genuine original post date) preferred over `updated_at`
  (bumped on any board-wide re-index; several unrelated jobs share the exact
  same `updated_at` timestamp, confirming it is not a reliable per-job
  signal on this tenant either).
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "cloudflare"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL_TMPL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.cloudflare.com/careers/",
}

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


def _job_posting_location(job: dict) -> str:
    """Return the authoritative location string for this job.

    Prefers the `metadata["Job Posting Location"]` entry (see module
    docstring) over the generic `location.name` field, which on this
    tenant is usually a work-mode label ("Hybrid"/"In-Office"/etc.), not a
    place.
    """
    for m in job.get("metadata") or []:
        if (m.get("name") or "") == "Job Posting Location":
            value = m.get("value")
            if isinstance(value, list):
                parts = [str(v).strip() for v in value if v]
                if parts:
                    return "; ".join(parts)
            elif isinstance(value, str) and value.strip():
                return value.strip()
    return ((job.get("location") or {}).get("name") or "").strip()


def _get_with_retry(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Cloudflare {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Cloudflare {what} failed: {exc}") from exc
    raise RateLimitError(f"Cloudflare {what}: no response -- {last_exc}")


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

        loc = _job_posting_location(job)
        if "india" not in loc.lower():
            continue

        app_url = job.get("absolute_url") or f"https://boards.greenhouse.io/{_BOARD_TOKEN}/jobs/{job_id}"

        _content_cache[job_id] = job.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_date(job),
            "application_url": app_url,
        })

    _india_cache[:] = collected
    print(f"[Cloudflare] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Cloudflare India jobs.

    Greenhouse's public job-list endpoint has no keyword/location query
    param and always returns the full current board, so the pool is
    fetched once and cached; keyword/location arguments are accepted for
    interface compatibility but not applied here (the shared title/skill
    filters in matcher.py do the real narrowing).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Cloudflare job."""
    m = re.search(r"/jobs/(\d+)", application_url or "")
    job_id = m.group(1) if m else ""

    if job_id and job_id in _content_cache:
        return _strip_html(_content_cache[job_id]), ""

    if not job_id:
        raise RateLimitError(f"Cloudflare description: could not parse job id from {application_url!r}")

    r = _get_with_retry(_DETAIL_URL_TMPL.format(job_id=job_id) + "?content=true", timeout, "description fetch")
    job = r.json()
    description = _strip_html(job.get("content") or "")
    posting_date = _parse_date(job)
    _content_cache[job_id] = job.get("content") or ""
    return description, posting_date
