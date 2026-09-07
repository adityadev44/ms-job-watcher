r"""Fetches MakeMyTrip (MMT) job listings via its own custom careers-site
API (careers.makemytrip.com), which itself proxies a Darwinbox tenant.

ATS discovery (live, 2026-09-07): careers.makemytrip.com is a React SPA
whose own backend exposes two clean, unauthenticated JSON endpoints
(confirmed via a live Playwright network-request capture — plain Chromium
hangs/times out navigating this SPA in this sandbox for unrelated reasons,
but headless Firefox loads it fine and plain `requests` to the two API
endpoints below works with zero cookies/headers beyond a normal UA):

    GET https://careers.makemytrip.com/api/jobs
        -> {"allJobs": [...], "businessUnits": [...], "locations": [...]}
    GET https://careers.makemytrip.com/api/jobDetails?jobId=<id>
        -> {"status": 1, "data": {"job_decription": "<double-HTML-escaped
             HTML>", "applyUrl": "https://gommt.darwinbox.in/ms/candidatev2/
             main/careers/jobDetails/<id>?from=all", ...}}

The `applyUrl` in the detail response reveals MMT's real underlying ATS is
its OWN Darwinbox tenant ("gommt") — same product family as
darwinbox_fetcher.py in this repo — but MMT has built its own branded
frontend + API wrapper around it (same "custom in-house wrapper over a
vendor ATS" shape as indigo_fetcher.py wrapping SuccessFactors). This
fetcher talks to MMT's own wrapper API directly rather than the raw
Darwinbox tenant: it's a plain unauthenticated same-origin API with no
Cloudflare/bot-management gating (confirmed live — ordinary `requests`
works), so no Playwright/browser session is needed anywhere in this
pipeline, unlike darwinbox_fetcher.py itself.

Server-side filtering — tested live: `/api/jobs?search=zzznonsense123`
returns byte-for-byte the same 24014-char response as no params at all —
keyword params are silently ignored. The whole board is small (37 open
postings, live 2026-09-07) and every single one is India-based
(`location_country` is always literally `"India"`), so the whole board is
cached once per process and matcher.py's shared filters do the real
narrowing (same policy as airindia_fetcher.py/indigo_fetcher.py for a small
board).

Posting date: each job carries both `job_created_timestamp` (varies widely,
e.g. 2022-2026 across the live board — the genuine original post date) and
`job_updated_timestamp` (nearly every job on the live board shows a
2026-09-07 updated timestamp, i.e. this tenant's Darwinbox sync appears to
touch `job_updated_timestamp` on every job on every scrape, making it
useless as a "when did this posting actually change" signal). This fetcher
therefore uses `job_created_timestamp` for `posting_date`, parsed from its
wire format `DD-MM-YYYY HH:MM:SS`.

Location: the `location` field is already a list of clean, human-readable
strings that already contain ", India" (e.g. `"Gurgaon, Haryana, India
(Gurgaon_MMT)"`) — only the trailing internal office-code parenthetical
(`" (Gurgaon_MMT)"`) is stripped; multiple locations on one req are joined
with "; ".

Description: `job_decription` (their field name, misspelled, sic) on the
`jobDetails` endpoint is HTML-entity-escaped ONE EXTRA LEVEL — raw text
starts `&lt;p ...&gt;` — same idiom as Darwinbox/Zomato/Razorpay/Groww/
Perfios/Sonata elsewhere in this repo: unescape, strip tags, unescape
again. This requires a separate per-job API call (unlike Darwinbox itself,
this wrapper's `/api/jobs` list endpoint does NOT inline the description).

Application URL: `https://careers.makemytrip.com/prod/opportunity/<id>/
<slug>` — confirmed live via Playwright click-through from the real jobs
listing page. `fetch_job_description` parses the id back out of this URL
(second-to-last path segment) to call `/api/jobDetails?jobId=<id>`.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests

_JOBS_API = "https://careers.makemytrip.com/api/jobs"
_DETAIL_API = "https://careers.makemytrip.com/api/jobDetails"
_OPPORTUNITY_BASE = "https://careers.makemytrip.com/prod/opportunity/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_PAGE_SIZE = 25

_job_cache: list[dict] = []
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


_CODE_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _location_from_job(j: dict) -> str:
    seen: set[str] = set()
    locs: list[str] = []
    for loc in j.get("location") or []:
        cleaned = _CODE_SUFFIX_RE.sub("", loc or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            locs.append(cleaned)
    if locs:
        return "; ".join(locs)
    country = (j.get("location_country") or "").strip()
    return country or "India"


def _parse_created_date(raw: str) -> str:
    """Convert 'DD-MM-YYYY HH:MM:SS' -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%d-%m-%Y %H:%M:%S").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _get_json(url: str, *, timeout: int = 20, context: str = "") -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"MakeMyTrip {context}: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"MakeMyTrip {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"MakeMyTrip {context}: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire MakeMyTrip board once and cache it.

    ``_cache_filled``/``_cache_error`` are set before/during the one real
    fetch so a failure never silently becomes an empty-but-"successful"
    cache on a later call in the same process (Honeywell/Persistent
    lesson — see airindia_fetcher.py).
    """
    global _cache_filled, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    try:
        data = _get_json(_JOBS_API, timeout=timeout, context="job list")
    except RateLimitError as exc:
        _cache_error = exc
        raise

    raw_jobs = data.get("allJobs") if isinstance(data, dict) else None
    if raw_jobs is None:
        _cache_error = RateLimitError("MakeMyTrip: /api/jobs response missing 'allJobs'")
        raise _cache_error

    collected: list[dict] = []
    for j in raw_jobs:
        if j.get("post_on_careers_page") != 1:
            continue
        job_id = str(j.get("job_id") or "").strip()
        title = (j.get("job_title") or "").strip()
        if not (job_id and title):
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "job"
        collected.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(j),
            "posting_date": _parse_created_date(j.get("job_created_timestamp") or ""),
            "application_url": f"{_OPPORTUNITY_BASE}{job_id}/{slug}",
        })

    _job_cache[:] = collected
    print(f"[MakeMyTrip] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a slice of MakeMyTrip's current open postings.

    ``keyword``/``location`` are accepted for interface compatibility but
    ignored — `/api/jobs` silently ignores every query param tried
    (confirmed live) and always returns the full (small) board; matcher.py's
    shared filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


_JOB_ID_RE = re.compile(r"/opportunity/([^/]+)/")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single MakeMyTrip job.

    Descriptions are NOT on the list endpoint — a separate
    `/api/jobDetails?jobId=<id>` call is required, whose `job_decription`
    field is HTML-escaped one extra level (see module docstring).
    """
    m = _JOB_ID_RE.search(application_url)
    job_id = m.group(1) if m else application_url.rstrip("/").rsplit("/", 1)[-1]

    data = _get_json(
        f"{_DETAIL_API}?jobId={job_id}", timeout=timeout, context=f"job detail {job_id}"
    )
    detail = data.get("data") if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return "", ""

    description = _strip_html(detail.get("job_decription") or "")
    posting_date = _parse_created_date(detail.get("job_created_timestamp") or "")
    return description, posting_date
