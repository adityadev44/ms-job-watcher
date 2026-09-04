"""Fetches AB InBev GCC (India) job listings via the Sense/Skillate career page.

ab-inbev-gcc.sensehq.com is a "Sense Talent Engagement Platform" (candidate
CRM/texting product) instance -- almost every route on the bare domain
(`/`, `/jobs`, ...) redirects to `/signin` and requires a login. The one
public, unauthenticated route is `/careers`, a separate embedded career-site
micro-site. Its own org config reports `"hosting_type":"SKILLATE"` -- Sense
acquired the Skillate ATS/career-page builder, and `/careers` is that
product's public job board, white-labelled under the sensehq.com domain.
This is a genuinely new ATS vendor combination for this repo (Sense CRM +
Skillate career-page backend), confirmed via live DevTools/network capture,
not from the prior "plausible SenseHQ" secondary-research guess alone.

`/careers` is server-rendered Next.js. Plain `requests` (no Playwright, no
signin) works for everything:
  - The initial HTML embeds a Next.js `buildId` (inside `__NEXT_DATA__`).
  - `GET /careers/_next/data/{buildId}/jobs.json?...` returns the same JSON
    the SPA itself fetches for pagination/filtering.
  - `GET /careers/_next/data/{buildId}/job.json?id={id}` returns full detail
    for a single job -- but the *list* response already inlines the full
    `description_external` HTML, so no per-job detail call is needed at all.

Key quirks discovered live (2026-09-03), not yet documented elsewhere in
this repo:
  - `buildId` is a per-deploy Next.js token, not a stable constant -- it is
    re-scraped from the live `/careers` HTML on every cache fill rather than
    hardcoded, so a redeploy can't silently 404 every request.
  - `pageSize` is effectively ignored: any value <= 10 still returns exactly
    5 rows; any value > 10 (15, 20, 50, ...) silently returns ZERO rows with
    HTTP 200 and a correct `count` field -- the same "silently no-ops over a
    hidden cap" shape already seen at Shell/Northern Trust, just triggered by
    `pageSize` here instead of `limit`.
  - Plain page-walking (`page=1,2,3,...` with a fixed sort) breaks after
    page 2: page>=3 always returns 0 rows even when the true total (`count`)
    says more jobs exist. The API can only reliably deliver the first 10
    results of any given sort order.
  - The `department` filter looks like it works -- its own `count` field
    updates correctly per department -- but `rows` comes back empty for
    every department value tried (name string and numeric `department_id`
    both fail identically). A new, independent backend bug: the count
    aggregation and the row query are evidently answered by different code
    paths server-side, and only one of them applies the filter.
  - Workaround for both bugs: fetch page 1+2 (5+5 rows) sorted
    `created_on ASC`, then again sorted `created_on DESC`, and union the
    two by job id. The oldest-10 and newest-10 views together cover the
    whole pool as long as it stays under ~20 postings (true today, a tiny
    GCC-specific board), same "cache the small pool once" discipline as
    MSCI/Perfios/Groww/Razorpay.
  - `location` on every job is a bare city name ("Bangalore" today, the
    only value the API's own `locations` facet list returns) with no
    "India" token -- appended via a small recognized-city whitelist rather
    than blindly, per the Lowe's/Invesco lesson, in case a future posting
    ever shows a non-India location.
  - Current job pool (15 postings, live-checked) is analytics/ops/finance
    heavy (Analyst, Specialist, Manager tiers) with a "GAC - GEN AI"
    department that mentions "GenAI" in prose but never any hard AI/ML
    `primary_skills` term (LangChain/RAG/vector DB/etc.) and never any
    .NET/C#/ASP.NET term -- a genuine current zero for both tracks, not a
    fetcher defect (same class as ING/eClerx/Pfizer/Boeing/PepsiCo/Disney).
"""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timezone

import requests

_ROOT = "https://ab-inbev-gcc.sensehq.com"
_CAREERS_URL = f"{_ROOT}/careers"
_JOBS_URL_TMPL = f"{_ROOT}/careers"  # application_url base: /careers/jobs/{id}

_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_CAREERS_URL}/",
}

# Only city ever observed in this tenant's own `locations` facet. Appended
# to bare city names so matcher.py's is_india_job() ("india" in location)
# passes; a future city not in this list is left untouched rather than
# blindly labelled India (Lowe's/Invesco lesson).
_KNOWN_INDIA_CITIES = {"bangalore", "bengaluru"}

# Module-level cache: the whole (tiny) India pool is fetched once per
# process and re-served for every keyword call, same pattern as
# MSCI/Perfios/Groww/Razorpay.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure after retries."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_epoch_ms(ms) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _normalize_location(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return raw
    if "india" in raw.lower():
        return raw
    if raw.lower() in _KNOWN_INDIA_CITIES:
        return f"{raw}, India"
    return raw


def _request_with_retries(url: str, *, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("AB InBev GCC: 429 rate-limited after retries")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"AB InBev GCC request failed: {exc}") from exc
    raise RateLimitError(f"AB InBev GCC request failed: {last_exc}")


def _get_build_id(timeout: int) -> str:
    r = _request_with_retries(_CAREERS_URL, timeout=timeout)
    m = _BUILD_ID_RE.search(r.text)
    if not m:
        raise RateLimitError("AB InBev GCC: could not locate Next.js buildId on /careers")
    return m.group(1)


def _fetch_jobs_page(build_id: str, *, page: int, order: str, timeout: int) -> list[dict]:
    url = (
        f"{_ROOT}/careers/_next/data/{build_id}/jobs.json"
        f"?page={page}&pageSize=5&department=&location=&title="
        f"&sortBy=created_on&orderBy={order}&minExp=0&maxExp=100"
        f"&jobType=&workplaceType="
    )
    r = _request_with_retries(url, timeout=timeout)
    payload = r.json()
    return payload.get("pageProps", {}).get("jobsData", {}).get("rows", []) or []


def _job_from_row(row: dict) -> dict | None:
    job_id = row.get("id")
    title = (row.get("title") or "").strip()
    if not job_id or not title:
        return None
    job_id = str(job_id)
    application_url = f"{_JOBS_URL_TMPL}/jobs/{job_id}"
    desc = _strip_html(row.get("description_external") or "")
    if desc:
        _desc_cache[application_url] = desc
    return {
        "id": job_id,
        "title": title,
        "location": _normalize_location(row.get("location") or ""),
        "posting_date": _parse_epoch_ms(row.get("created_on")),
        "application_url": application_url,
    }


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the whole (tiny) job pool once, unioning two sort directions
    to work around the >page-2 pagination bug (see module docstring).

    _cache_filled is set before the network calls so a failure on the very
    first request doesn't trigger a retry storm on every subsequent
    fetch_jobs() call within the same process (Honeywell lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    build_id = _get_build_id(timeout)

    by_id: dict[str, dict] = {}
    for order in ("ASC", "DESC"):
        for page in (1, 2):
            rows = _fetch_jobs_page(build_id, page=page, order=order, timeout=timeout)
            if not rows:
                break
            for row in rows:
                job = _job_from_row(row)
                if job is not None:
                    by_id[job["id"]] = job
            time.sleep(0.15)

    _job_cache.clear()
    _job_cache.extend(by_id.values())
    print(f"[AB InBev GCC] Cache filled: {len(_job_cache)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of AB InBev GCC India jobs.

    Keywords are ignored -- the site's own `title=` search param would only
    narrow an already-tiny (~15 job) pool further, and the shared
    title/skill filters in matcher.py do the real work. The whole pool is
    cached once per process (see _fill_cache).
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single AB InBev GCC job.

    Descriptions are cached during fetch_jobs (inlined in the list
    response) so no additional HTTP call is needed; posting_date is
    already set from the search result, so it's returned empty here.
    """
    return _desc_cache.get(application_url, ""), ""
