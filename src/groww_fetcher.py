"""Fetches Groww job listings via the Greenhouse ATS.

careers.groww.in ("Careers" link on groww.in) embeds a Greenhouse job board
widget pointing at job-boards.eu.greenhouse.io/groww -- confirmed by fetching
https://groww.in/careers directly and finding
`href="https://job-boards.eu.greenhouse.io/groww"` in the page HTML. The
board token is "groww" and the public Greenhouse Job Board API works
regardless of which regional embed subdomain (eu vs default) the careers
page itself uses:

    GET https://boards-api.greenhouse.io/v1/boards/groww/jobs?content=true

Verified live on 2026-08-30: returns HTTP 200 with real, current Groww
postings (job IDs, titles, India office locations, full HTML `content`).

Key quirks:
- Greenhouse's job-list endpoint returns the ENTIRE current board in one
  call -- no server-side pagination or keyword filtering (Greenhouse always
  ignores any query params other than `content`). All jobs are fetched once
  and cached in-module, the same "cache-once" pattern as
  persistent_fetcher.py / deutsche_fetcher.py / msci_fetcher.py /
  hexaware_fetcher.py. `_cache_filled` is set to True *before* the fetch
  attempt (Honeywell lesson) so a transient failure doesn't trigger a retry
  storm across every keyword call in the same process. fetch_jobs() then
  slices [start:start+num] from the cache.
- India filtering is done INSIDE the cache fill (case-insensitive "india"
  substring on `location.name`), matching persistent/hexaware/deutsche/msci
  rather than the WTW-style "return everything ungated, let matcher.py's
  is_india_job() decide" approach. Groww is an India-headquartered company
  and its board is small, so nearly every posting is already India-located
  anyway; filtering here keeps this fetcher consistent with the majority of
  "ignores keywords, cache once" fetchers in this repo.
- Greenhouse's documented list-response fields give only `updated_at`
  (bumped on any edit, not just re-posts). This response also happens to
  include an undocumented `first_published` field that is a genuinely
  earlier, more accurate original-posting timestamp than `updated_at` for
  every job observed live. Since it's free from the same response and
  strictly more accurate for `posting_date`, it's used when present, falling
  back to `updated_at` (truncated to YYYY-MM-DD) otherwise -- this is a
  deliberate deviation from "just use updated_at", made after confirming the
  field's presence and behavior against the live API rather than assuming.
- `content` (full HTML job description) is already included in the list
  response with `?content=true` -- no separate per-job detail call is
  needed for the common path. fetch_job_description() serves from the
  in-module cache; if called for a job ID that isn't cached (cache not yet
  filled, or the job closed after caching), it falls back to Greenhouse's
  single-job detail endpoint
  (`/v1/boards/groww/jobs/{id}?content=true`), which uses the same shape.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "groww"
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
    "Referer": "https://groww.in/careers",
}

# Module-level cache: filled once, reused for all keyword calls.
_india_cache: list[dict] = []
_content_cache: dict[str, str] = {}   # job_id (str) -> raw HTML content
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Greenhouse."""


def _strip_html(raw: str) -> str:
    """Strip HTML tags from Greenhouse's `content` field.

    Unlike most fetchers in this repo, Groww's Greenhouse `content` comes
    HTML-entity-encoded (e.g. "&lt;div&gt;...&lt;/div&gt;" rather than a raw
    "<div>...</div>"), so tags are invisible to a naive tag-strip regex until
    entities are decoded first. Unescaping before stripping handles both
    the encoded case (this ATS) and the plain-HTML case (everyone else)
    identically, since unescaping already-plain text is a no-op for `<`/`>`.
    """
    text = _html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_date(job: dict) -> str:
    """Prefer first_published (genuine original post date) over updated_at
    (bumped on any edit); both are ISO8601 -- truncate to YYYY-MM-DD."""
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
                raise RateLimitError(f"Groww {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Groww {what} failed: {exc}") from exc
    raise RateLimitError(f"Groww {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Groww Greenhouse board once and cache India jobs.

    _cache_filled is set True before the request is attempted so a failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() call in
    this process (Honeywell/persistent lesson).
    """
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

        app_url = job.get("absolute_url") or f"https://job-boards.eu.greenhouse.io/{_BOARD_TOKEN}/jobs/{job_id}"

        _content_cache[job_id] = job.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_name or "India",
            "posting_date": _parse_date(job),
            "application_url": app_url,
        })

    _india_cache = collected
    print(f"[Groww] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Groww India jobs.

    Greenhouse ignores keyword/location query params server-side and always
    returns the full current board, so the pool is fetched once and cached;
    keyword/location arguments are accepted for interface compatibility but
    not applied here (the shared title/skill filters in matcher.py do the
    real narrowing).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Groww job.

    Parses the job ID out of the trailing path segment of application_url
    (Greenhouse absolute_url is always .../jobs/{id}). Serves content from
    the in-module cache when available; otherwise falls back to Greenhouse's
    single-job detail endpoint, which returns the same shape as the list
    endpoint's per-job entries.
    """
    m = re.search(r"/jobs/(\d+)", application_url or "")
    job_id = m.group(1) if m else ""

    if job_id and job_id in _content_cache:
        return _strip_html(_content_cache[job_id]), ""

    if not job_id:
        raise RateLimitError(f"Groww description: could not parse job id from {application_url!r}")

    r = _get_with_retry(_DETAIL_URL_TMPL.format(job_id=job_id) + "?content=true", timeout, "description fetch")
    job = r.json()
    description = _strip_html(job.get("content") or "")
    posting_date = _parse_date(job)
    _content_cache[job_id] = job.get("content") or ""
    return description, posting_date
