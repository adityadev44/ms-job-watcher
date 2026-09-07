"""Fetches GitLab job listings via the Greenhouse ATS.

GitLab's careers page (about.gitlab.com/jobs) is Greenhouse-backed, board
token "gitlab". Confirmed live 2026-09-06:

    GET https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true

-- HTTP 200, 229 total postings globally. Same "cache-once, ignores
keywords/location" family as every other Greenhouse board in this repo --
the public job-list endpoint has no keyword/location query param and always
returns the identical full board (verified: `&q=zzznonsensequeryabc123`
returns the same 229 jobs), so the whole board is fetched once, cached
in-module, and India jobs are filtered during the fill.

Location: unlike MongoDB/Cloudflare (bare city names / work-mode labels),
GitLab's `location.name` already contains the literal substring "india" for
every India posting observed -- e.g. "Bangalore, India", "Remote, India",
"Remote, India; Remote, Phillipines" -- so no city-whitelist normalization
hack is needed here; `location.name` is used as-is.

Verified live 2026-09-06: 40 India-located postings (of 229 global; the
single largest India count of the three Greenhouse boards onboarded in this
batch), overwhelmingly genuine hands-on engineering titles at every level:
"Senior Software Engineer", "Staff Software Engineer" (x2), "Staff Backend
Engineer" (x5, one "(Go)"), "Senior Backend Engineer" (x5), "Intermediate
Backend Engineer" (x3), "Senior/Staff Site Reliability Engineer", "Senior
Software Security Engineer", "Staff Infrastructure Security Engineer",
"RPA Engineer, UiPath" -- almost entirely Bangalore-based, all under
GitLab's real product-engineering org (Cell Infrastructure, Database Change
Management, Software Supply Chain Security, Data Products, Monetization).
This is the strongest India engineering signal of the three Greenhouse
boards in this batch by a wide margin.

Key quirks (same double-encoding family already documented for MongoDB/
Cloudflare/Glean/Groww/Razorpay/AlphaSense):
- `content` (full HTML JD) is unescaped twice before tag-stripping.
- `first_published` (genuine original post date) preferred over `updated_at`.
- A few reqs list Bangalore alongside several Remote-* countries in one
  semicolon-joined `location.name` string (e.g. "Bangalore, India; Remote,
  Canada; Remote, Israel; Remote, United Kingdom; Remote, United States") --
  kept as-is; the "india" substring check still passes correctly since
  Bangalore's own segment already says "India".
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "gitlab"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL_TMPL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://about.gitlab.com/jobs/",
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


def _get_with_retry(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"GitLab {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"GitLab {what} failed: {exc}") from exc
    raise RateLimitError(f"GitLab {what}: no response -- {last_exc}")


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
        if "india" not in loc_name.lower():
            continue

        app_url = job.get("absolute_url") or f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs/{job_id}"

        _content_cache[job_id] = job.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_name,
            "posting_date": _parse_date(job),
            "application_url": app_url,
        })

    _india_cache[:] = collected
    print(f"[GitLab] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of GitLab India jobs.

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
    """Return (description_text, posting_date) for a single GitLab job."""
    m = re.search(r"/jobs/(\d+)", application_url or "")
    job_id = m.group(1) if m else ""

    if job_id and job_id in _content_cache:
        return _strip_html(_content_cache[job_id]), ""

    if not job_id:
        raise RateLimitError(f"GitLab description: could not parse job id from {application_url!r}")

    r = _get_with_retry(_DETAIL_URL_TMPL.format(job_id=job_id) + "?content=true", timeout, "description fetch")
    job = r.json()
    description = _strip_html(job.get("content") or "")
    posting_date = _parse_date(job)
    _content_cache[job_id] = job.get("content") or ""
    return description, posting_date
