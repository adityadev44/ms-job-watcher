"""Fetches Okta job listings via the Greenhouse ATS.

Okta's careers page (okta.com/company/careers) is Greenhouse-backed, board
token "okta". Confirmed live 2026-09-13:

    GET https://boards-api.greenhouse.io/v1/boards/okta/jobs?content=true

-- HTTP 200, ~324 total postings globally. Same "cache-once, ignores
keywords" family as MongoDB/Zscaler/Glean/Groww/Razorpay/AlphaSense:
Greenhouse's public job-list endpoint has no keyword/location query param,
returns the ENTIRE current board in one call, so the whole board is fetched
once, cached in-module, and India jobs are filtered during the cache fill.

Location is already a clean, unambiguous "Bengaluru, India" string for
every India posting -- no bare-city or country-code normalization quirk
here (unlike MongoDB's bare "Gurugram"/"Bengaluru" or Zscaler's bare "IND"
abbreviation).

Verified live 2026-09-13: 108 India postings (of 324 global), all
Bengaluru -- Okta's India Innovation Centre. Very strong genuine
engineering signal: "Principal Software Engineer, AI Engineering",
multiple "Staff/Senior Software Engineer" reqs, "Senior Fullstack Engineer
(Java + React.js)", "Staff Site Reliability Engineer", "Senior Data
Engineer", "Staff DevSecOps Engineer".
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "okta"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL_TMPL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.okta.com/company/careers/",
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
                raise RateLimitError(f"Okta {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Okta {what} failed: {exc}") from exc
    raise RateLimitError(f"Okta {what}: no response -- {last_exc}")


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
    print(f"[Okta] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Okta India jobs.

    Greenhouse's public job-list endpoint has no keyword/location query
    param and always returns the full current board, so the pool is
    fetched once and cached; keyword/location arguments are accepted for
    interface compatibility only.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start: start + num]


def _job_id_from_url(application_url: str) -> str:
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
    job_id = _job_id_from_url(application_url)

    if job_id and job_id in _content_cache:
        return _strip_html(_content_cache[job_id]), ""

    if not job_id:
        raise RateLimitError(f"Okta description: could not parse job id from {application_url!r}")

    r = _get_with_retry(_DETAIL_URL_TMPL.format(job_id=job_id) + "?content=true", timeout, "description fetch")
    job = r.json()
    description = _strip_html(job.get("content") or "")
    posting_date = _parse_date(job)
    _content_cache[job_id] = job.get("content") or ""
    return description, posting_date
