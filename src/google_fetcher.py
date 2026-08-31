"""Fetches Google job listings from careers.google.com's server-rendered
search results — no XHR/internal API reverse-engineering required.

Google shut down its public careers API in 2021 and there is still no
documented public API today. However, careers.google.com/jobs/results
redirects (301) to https://www.google.com/about/careers/applications/jobs/results/,
a "wiz"-framework SPA that SERVER-RENDERS the full first batch of matching
jobs directly into an `AF_initDataCallback({key: 'ds:1', ...})` JSON blob in
the initial HTML — the same pattern Google uses on Search/Flights/etc. The
SPA's own client-side JS reads this exact blob to paint the page; a plain
`requests.get` on the same URL gets the same blob, no browser needed.

Verified live (2026-08-31) via plain unauthenticated GET, no session/cookies,
no CAPTCHA, no JS execution:
  - `?location=India` alone returns all 282 India jobs, 20/page, and the
    `location` filter is genuinely server-side and accurate (every sampled
    job's own location fields say "<City>, <State>, India" — no leakage).
  - `?q=<keyword>` genuinely narrows results server-side (confirmed the
    reported total job count changes, e.g. 282 -> 165 for
    "software engineer") — not a client-side no-op.
  - `?page=N` pagination is exact and non-overlapping: page 15 of the
    location=India query correctly returns the final 2 of 282 jobs with
    zero ID overlap against any other page.
  - Fetched all 282 India jobs across 15 sequential requests with no
    blocking, no rate-limit response, no bot challenge of any kind.
  - Each job's data blob already carries full HTML for "about the job",
    responsibilities, minimum qualifications, and preferred
    qualifications — no separate detail-page fetch is needed to run the
    skill/tech filters, same pattern as MSCI/Cognizant/S&P Global Careers
    (see `_INLINE_DESCRIPTIONS` in company_registry.py).
  - The public job-detail page also renders with a plain GET, no auth, at
    `https://www.google.com/about/careers/applications/jobs/results/{id}`
    (no slug needed in the path) — used as `application_url` so alert
    links land on a real, human-viewable job page rather than the
    embedded "Apply" link, which points to an authenticated
    `/applications/signin?jobId=...` URL instead.

Confirmed real matches exist in current India postings on both tracks —
e.g. several "Staff Software Engineer, ... Agentic AI Engineering" /
"GenAI" roles hit the AI/ML/Python primary_skills group via
"generative ai"/"large language model", and a few "Software Engineer,
Full Stack" roles hit the .NET/C# group via "C#" (Google full-stack
postings often list C# among acceptable languages).
"""
from __future__ import annotations

import html
import re
import time
import warnings
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

_BASE_URL = "https://www.google.com/about/careers/applications/jobs/results/"
_PAGE_SIZE = 20  # Google's own fixed page size; matcher.py also pages in 20s.

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# application_url -> plain-text description, populated as fetch_jobs() parses
# each page (the search response already carries the full JD, so no
# separate detail request is needed — see module docstring).
_desc_cache: dict[str, str] = {}

# (keyword, location) -> last known total-result count for that query. The
# final page of a query is usually a partial page (< 20 results), and
# matcher.py's caller advances `start` by the *actual* count returned each
# call — so after a partial last page, `start` is no longer a multiple of
# 20 and naive `start // 20 + 1` page math would re-request (or skip) a
# page instead of cleanly terminating. Once a query's total is known,
# short-circuit to [] as soon as start reaches it, without another request.
_total_cache: dict[tuple[str, str], int] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent failure fetching careers.google.com."""


def _find_ds1_block(page_html: str) -> str | None:
    """Return the raw `data:[...]` array text from the `ds:1` AF_initDataCallback block."""
    for m in re.finditer(r"AF_initDataCallback\((\{.*?\})\);", page_html, re.DOTALL):
        block = m.group(1)
        if "'ds:1'" not in block and '"ds:1"' not in block:
            continue
        try:
            data_idx = block.index("data:")
            start = block.index("[", data_idx)
        except ValueError:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(block)):
            c = block[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return block[start : i + 1]
    return None


def _text_field(field) -> str:
    """Fields carrying rich text are `[null, "<p>...</p>"]`; plain lists/None otherwise."""
    if isinstance(field, list) and len(field) > 1 and isinstance(field[1], str):
        return field[1]
    if isinstance(field, str):
        return field
    return ""


def _strip_html(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(ts_field) -> str:
    """`[seconds, nanos]` pair -> "YYYY-MM-DD"; best-effort."""
    if not isinstance(ts_field, list) or not ts_field:
        return ""
    try:
        seconds = int(ts_field[0])
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def _location_string(loc_field) -> str:
    if not isinstance(loc_field, list) or not loc_field:
        return "India"
    names = []
    for entry in loc_field:
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            names.append(entry[0])
    joined = "; ".join(names) if names else "India"
    return joined if "india" in joined.lower() else f"{joined}, India"


def _parse_jobs(data) -> tuple[list[dict], int]:
    """Return (job dicts, reported total) from a parsed `ds:1` payload."""
    jobs_raw = data[0] if data and isinstance(data[0], list) else []
    total = data[2] if len(data) > 2 and isinstance(data[2], int) else len(jobs_raw)

    jobs: list[dict] = []
    for j in jobs_raw:
        if not isinstance(j, list) or len(j) < 10:
            continue
        job_id = j[0]
        title = j[1]
        if not job_id or not title:
            continue
        application_url = f"{_BASE_URL}{job_id}"

        description = " ".join(
            filter(
                None,
                (
                    _strip_html(_text_field(j[10] if len(j) > 10 else None)),  # about the job
                    _strip_html(_text_field(j[3])),   # responsibilities
                    _strip_html(_text_field(j[4])),   # minimum qualifications
                    _strip_html(_text_field(j[19] if len(j) > 19 else None)),  # preferred qualifications
                ),
            )
        )
        _desc_cache[application_url] = description

        jobs.append(
            {
                "id": str(job_id),
                "title": title,
                "location": _location_string(j[9] if len(j) > 9 else None),
                "posting_date": _parse_date(j[12] if len(j) > 12 else None),
                "application_url": application_url,
            }
        )
    return jobs, total


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    cache_key = (keyword, location)
    known_total = _total_cache.get(cache_key)
    if known_total is not None and start >= known_total:
        return []

    page = start // _PAGE_SIZE + 1
    params = {"location": location or "India", "page": page}
    if keyword:
        params["q"] = keyword
    url = f"{_BASE_URL}?{urlencode(params)}"

    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise RateLimitError("Google careers: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise RateLimitError(f"Google careers fetch failed: {exc}") from exc

    block = _find_ds1_block(r.text)
    if block is None:
        # No embedded data on this page (e.g. paged past the last result) —
        # treat as end-of-results, same as an empty JSON page elsewhere.
        return []

    import json

    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise RateLimitError(f"Google careers: unparseable results blob: {exc}") from exc

    jobs, total = _parse_jobs(data)
    _total_cache[cache_key] = total
    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) — description comes from the
    in-module cache populated by fetch_jobs(); posting_date is left blank
    since fetch_jobs() already sets it from the search response.
    """
    return _desc_cache.get(application_url, ""), ""
