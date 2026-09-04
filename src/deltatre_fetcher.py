"""Fetches Deltatre job listings from the company's own careers site.

Deltatre (deltatre.com) is a global sports-media-technology company. Its
careers section (`https://www.deltatre.com/about/careers/open-positions`) is
a Next.js App Router page whose full list of currently-open positions is
server-rendered directly into the initial HTML response -- confirmed via a
plain `curl` (no JS execution) returning all 42 current postings (titles,
countries, per-job detail links) in the raw response body. There is no
separate ATS search API involved in reading job data: no `greenhouse.io`,
`lever.co`, `myworkdayjobs.com`, `smartrecruiters.com`, etc. anywhere in the
page's script/asset URLs, and no XHR/fetch call is needed to see the listing.

The one place a third-party ATS does appear is the *apply* flow: each job
detail page embeds an `<iframe src="https://deltatre.intervieweb.it/app.php
?...">` for candidate registration ("Intervieweb", an Italian recruiting
platform new to this repo). That ATS is never used for reading job data --
both the listing and the full job description (Position + Requirements
sections) are already server-rendered as plain HTML on deltatre.com itself,
so this fetcher never touches intervieweb.it.

Key quirks:
- No keyword or location query parameter exists on the listing page --
  every request returns the identical full pool regardless of query string.
  All jobs are fetched once and cached in-module (same "cache-once" pattern
  as groww_fetcher.py / razorpay_fetcher.py), then filtered to India via a
  case-insensitive "india" substring on the location text -- matcher.py's
  own `is_india_job()` re-checks this too, so this is belt-and-suspenders,
  not the sole gate.
- The location field on both the listing and the job-detail page is only
  ever a bare country name ("India", "Italy", "North Macedonia", "United
  Kingdom", "Toronto", or occasionally unset) -- never a city. All India
  postings observed on 2026-09-03 (8 of 42) explicitly name "the Mumbai
  office in Andheri" (or Goregaon) in the JD body text itself, so there is
  no evidence of a Pune/Chennai/other-excluded-city presence to worry about
  even though the location string itself could never trigger the
  city-based `exclude_locations` check.
- No posting-date field is exposed anywhere on the site (listing or detail
  page) -- same situation as ibm_fetcher.py / cisco_fetcher.py /
  unitedairlines_fetcher.py. Both fetch_jobs() and fetch_job_description()
  return "" for posting_date rather than guessing one.
- Job descriptions are NOT inline in the listing page -- only title,
  country, and the numeric job id are available there. The full description
  (Position + Requirements, rendered as two `<div class="d3com-markdown">`
  blocks) requires one GET per job's own detail page
  (`/about/careers/open-positions/{id}`), fetched lazily by
  fetch_job_description() the same as any non-inline-description fetcher.
"""
from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

_BASE = "https://www.deltatre.com"
_LISTING_URL = f"{_BASE}/about/careers/open-positions"
_JOB_URL_PREFIX = "/about/careers/open-positions/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Module-level cache: filled once, reused for all keyword/location calls.
_india_cache: list[dict[str, str]] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from deltatre.com."""


def _get_with_retry(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Deltatre {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Deltatre {what} failed: {exc}") from exc
    raise RateLimitError(f"Deltatre {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Deltatre open-positions listing once and cache India jobs.

    `_cache_filled` is set True before the request is attempted so a
    transient failure doesn't trigger a retry storm on every subsequent
    fetch_jobs() call in this process (Honeywell/persistent lesson).
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = _get_with_retry(_LISTING_URL, timeout, "cache fill")
    soup = BeautifulSoup(r.text, "html.parser")

    total = 0
    collected: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for a in soup.select(f'li a[href^="{_JOB_URL_PREFIX}"]'):
        href = a.get("href", "")
        job_id = href[len(_JOB_URL_PREFIX):].strip("/")
        if not job_id or not job_id.isdigit() or job_id in seen_ids:
            continue

        h4 = a.find("h4")
        title = h4.get_text(strip=True) if h4 else ""
        span = a.find("span")
        loc = span.get_text(strip=True) if span else ""
        if not title:
            continue

        seen_ids.add(job_id)
        total += 1

        if "india" not in loc.lower():
            continue

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc or "India",
            "posting_date": "",
            "application_url": f"{_BASE}{href}",
        })

    _india_cache = collected
    print(f"[Deltatre] Cache filled: {len(collected)} India jobs (of {total} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Deltatre India jobs.

    deltatre.com's listing page ignores keyword/location query params (there
    are none to send -- it always server-renders the full current pool), so
    the pool is fetched once and cached; keyword/location arguments are
    accepted for interface compatibility but not applied here.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Deltatre job.

    The description is not included in the listing response -- this fetches
    the job's own detail page and concatenates the text of every
    `d3com-markdown` block (Position, Requirements, etc). No posting-date
    field exists anywhere on the site, so posting_date is always "".
    """
    r = _get_with_retry(application_url, timeout, "description fetch")
    soup = BeautifulSoup(r.text, "html.parser")

    blocks = soup.find_all("div", class_="d3com-markdown")
    description = " ".join(
        " ".join(block.get_text(separator=" ").split()) for block in blocks
    ).strip()

    return description, ""
