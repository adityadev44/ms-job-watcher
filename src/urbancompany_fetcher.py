"""Urban Company job fetcher — first-party JSON API, no third-party ATS.

ATS discovery (2026-09-14): `careers.urbancompany.com/jobs` is a small React
SPA (webpack bundle named "career-page") with no Greenhouse/Lever/Workday/
Darwinbox/etc. vendor markers anywhere. A live Playwright network capture of
the page load shows exactly one XHR:

    POST https://www.urbanclap.com/api/v2/platform-gateway/getAllJobs

`urbanclap.com` is Urban Company's legacy/internal domain name (the company
rebranded from "UrbanClap" to "Urban Company" in 2020 but kept the old
domain for backend infrastructure — the same "old brand name survives in
the API host" pattern as several other companies in this repo, just for a
first-party API rather than an ATS tenant). No auth, no request body, no
query params needed — a cold, header-only `requests.post()` (a plain GET
also is not required; the frontend always uses POST with an empty body)
returns the full JSON job pool directly.

Response shape: `{"jobs": [{"job_id", "job_code", "parent_department",
"location": [...], "location_city": [...], "job_title",
"job_description": "<html>", ...}]}`. The full HTML description is already
embedded inline in the list response — no separate per-job detail endpoint
exists or is needed (same "everything inline" shape as `affine_fetcher.py`'s
SenseHQ board), so `fetch_job_description` just re-fetches (and re-caches)
the same small payload rather than hitting a second endpoint.

**Current live state (2026-09-14): 11 total open postings company-wide,
zero software engineering titles.** All 11 are Category Manager / Training
Manager / Corporate roles across Chennai, Ahmedabad, Hyderabad, Mumbai,
Delhi/Gurugram/Noida, and Bengaluru — a real, working, first-party pipeline
with a genuinely non-engineering current pool, not a fetcher defect. Onboarded
anyway (same precedent as BigBasket/Zomato/Volvo Cars in this repo): the
mechanism is proven end-to-end against 11 real, live postings with full
inline descriptions, and any future Urban Company SDE/engineering
requisition posted to this same endpoint will be picked up automatically.

No pagination parameters exist or are needed at this pool size — the whole
response is cached once per process, consistent with every other small
"cache-once" board in this repo.

Location handling: `location` is already a list of full "City, State,
India" strings (e.g. "Chennai, Tamil Nadu, India", or several combined for
a multi-site req like "Delhi, India; Gurugram, Haryana, India; Noida, Uttar
Pradesh, India") — joined with "; " the same way this repo's other
multi-location fetchers do, and left as-is otherwise since every observed
value already names a real Indian state or "India" outright with no
ambiguous "N Locations" placeholder shape to resolve.

No posting-date field exists anywhere in the response (`job_code`,
`job_id`, and department/location fields only) — `posting_date` is left as
an empty string, same handling as any other source in this repo with no
reliable date field (the shared matcher and notifier both tolerate this).
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_API_URL = "https://www.urbanclap.com/api/v2/platform-gateway/getAllJobs"
_CAREERS_PAGE = "https://careers.urbancompany.com/"
_JOB_PAGE_BASE = "https://careers.urbancompany.com/jobs/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": _CAREERS_PAGE,
}

_job_cache: list[dict] = []
_description_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _fetch_pool(timeout: int) -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(_API_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Urban Company: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Urban Company fetch failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Urban Company: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    data = _fetch_pool(timeout)
    raw_jobs = (data or {}).get("jobs") or []

    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("job_id") or "").strip()
        title = (j.get("job_title") or "").strip()
        if not (job_id and title):
            continue

        locs = [str(l).strip() for l in (j.get("location") or []) if str(l).strip()]
        loc_str = "; ".join(locs) if locs else "India"

        _description_cache[job_id] = j.get("job_description") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_str,
            "posting_date": "",
            "application_url": f"{_JOB_PAGE_BASE}{job_id}",
        })

    _job_cache[:] = collected
    print(f"[Urban Company] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Urban Company postings from the cached full pool.

    keyword/location are accepted for interface compatibility but ignored —
    the frontend's own `getAllJobs` call takes no filter params; the whole
    (small) board is fetched and cached once per process.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Urban Company job.

    The description is already embedded inline in the list response (see
    module docstring); this re-fills the cache if needed (first call in a
    process) rather than hitting a separate endpoint, since none exists.
    """
    _fill_cache(timeout=timeout)
    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]
    description = _strip_html(_description_cache.get(job_id, ""))
    return description, ""
