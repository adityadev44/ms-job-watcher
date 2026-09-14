"""Fetches Zynga job listings via the Greenhouse ATS.

Zynga's branded domain (`www.zynga.com`, and its `jobs.zynga.com` redirect
target) was unreachable over plain HTTP from this environment during
investigation (connection timeouts on every request, including via a
server-side fetch tool on a different network path) despite DNS resolving
fine — a network-level block/outage unrelated to the ATS itself. The real
ATS was confirmed via live web search results referencing
`job-boards.greenhouse.io/zyngacareers` job-detail URLs (Zynga's standard
careers board; a separate `zyngaearlycareers` board exists for interns/new
grads only and was not used here). The public Greenhouse Job Board API
works independent of the branded-domain reachability issue:

    GET https://boards-api.greenhouse.io/v1/boards/zyngacareers/jobs?content=true

Verified live 2026-09-14: HTTP 200, 43 total postings, 7 in
"Bengaluru, India" (Zynga's real, substantial India studio — Zynga's own
blog documents "Zynga Expands its India Studio and Moves into New Office
in Bengaluru"; Zynga is a Take-Two Interactive subsidiary but retains its
own Greenhouse board, separate from Take-Two's).

Key quirks (same "cache-once" pattern as groww_fetcher.py — same ATS,
nearly identical shape):
- Greenhouse's job-list endpoint returns the entire current board in one
  call; keyword/location query params are ignored server-side. All jobs
  fetched once and cached in-module, `_cache_filled` set True before the
  fetch attempt (Honeywell lesson) to avoid a retry storm.
- India filtering is a case-insensitive "india" substring on
  `location.name` inside the cache fill, same as groww_fetcher.py.
- `content=true` embeds the full HTML job description directly in the list
  response — no separate per-job detail call needed for the common path;
  `fetch_job_description` falls back to Greenhouse's single-job detail
  endpoint only if called for an ID not present in the cache.
- `first_published` (present but undocumented) is preferred over
  `updated_at` for `posting_date`, same rationale as Groww: it is the
  genuine original-posting timestamp rather than a last-edited timestamp.
- **Current India postings are 100% non-engineering** (Lead Game Designer,
  Lead Producer, Principal Data Analyst, Producer, Product Manager II x2,
  Senior 2D Animator) as of this integration — 0 matches expected today,
  not a fetcher defect. Zynga is a large, actively-hiring studio with a
  churning 43-job board; a genuine SDE/AI-ML opening in Bengaluru is a
  realistic near-term event, same "feasible ATS, temporarily zero matches"
  shape as Icertis/BNY Mellon/Ubisoft(Pune) rather than a reason to skip.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "zyngacareers"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL_TMPL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://job-boards.greenhouse.io/zyngacareers",
}

_india_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Greenhouse."""


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(raw or "")
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
                raise RateLimitError(f"Zynga {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zynga {what} failed: {exc}") from exc
    raise RateLimitError(f"Zynga {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _india_cache, _cache_filled
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
            "location": loc_name or "India",
            "posting_date": _parse_date(job),
            "application_url": app_url,
        })

    _india_cache = collected
    print(f"[Zynga] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    _fill_cache(timeout=timeout)

    m = re.search(r"/jobs/(\d+)", application_url)
    job_id = m.group(1) if m else ""

    if job_id in _content_cache:
        for job in _india_cache:
            if job["id"] == job_id:
                return _strip_html(_content_cache[job_id]), job["posting_date"]

    if not job_id:
        return "", ""

    r = _get_with_retry(
        f"{_DETAIL_URL_TMPL.format(job_id=job_id)}?content=true", timeout, "detail fetch"
    )
    job = r.json()
    return _strip_html(job.get("content") or ""), _parse_date(job)
