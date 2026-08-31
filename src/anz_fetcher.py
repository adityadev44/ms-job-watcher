"""Fetches ANZ (Australia and New Zealand Banking Group) India job listings
via the SAP SuccessFactors J2W (classic) HTML search API.

careers.anz.com is the same classic J2W platform as Nomura/Capgemini/SAP
Labs/Mastek (real server-rendered `<tr class="data-row">` rows, no client-side
JS required) — reachable with plain `requests`, no Playwright.

Key quirks:
- `GET /search/?q=&locationsearch=india` reliably scopes results to India
  server-side (all current postings are Bengaluru); `q` keyword narrowing is
  a loose/unreliable pre-filter (e.g. a nonsense query still returned 5 of
  the 20 India jobs) — ignored here, matching the "loose pre-filter" pattern
  documented for Nagarro/PhonePe. The full India pool (~20 jobs) is cached
  once per process and re-sliced for every keyword call, same as
  `persistent_fetcher.py`.
- Pagination is `?startrow=N`, like Capgemini (not Nomura's path-based
  `/9050900/100/` scheme). CAUTION: requesting `startrow` at or beyond the
  true India-filtered total does NOT return an empty page — it silently
  *drops* the `locationsearch=india` filter and returns a generic/default
  slice of unrelated global (AU/PH) jobs instead, with its own unrelated
  "Results X of Z" total. `_fill_cache` reads the authoritative total from
  the first page's own "Results 1 – Y of Z" label and never requests a
  `startrow` >= that Z, so this wraparound is never triggered.
- Detail pages are plain server-rendered HTML: `<span class="jobdescription">`
  for the JD body and `<meta itemprop="datePosted" content="Wed Aug 19
  00:00:00 UTC 2026">` for the date — identical shape to Capgemini/Nomura.
- Location format on both search rows and detail pages is "Bengaluru, IN" —
  normalised to "Bengaluru, India" so matcher.py's `is_india_job()` matches.
"""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.anz.com"
_SEARCH_URL = f"{_BASE_URL}/search/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_RESULTS_RE = re.compile(r"Results\s*<b>(\d+)\s*.\s*(\d+)</b>\s*of\s*<b>(\d+)</b>")


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from ANZ's J2W search."""


# Module-level cache: the whole India pool is small (~20 jobs) and keyword
# narrowing is unreliable, so it's fetched once and re-sliced for every
# keyword call — same pattern as persistent_fetcher.py / Deutsche Bank.
# _cache_filled is set *before* the fetch loop so a failure doesn't trigger
# a retry storm on every subsequent keyword call within the same run
# (Honeywell/Persistent lesson).
_anz_cache: list[dict] = []
_cache_filled: bool = False


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert '31 Aug 2026' (search result) to '2026-08-31'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Wed Aug 19 00:00:00 UTC 2026' (meta tag) to '2026-08-19'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Search page fetch + parse
# ---------------------------------------------------------------------------

def _fetch_page(start: int, timeout: int) -> tuple[list[dict], int | None]:
    """Fetch one India-filtered search page starting at `start`.

    Returns (jobs_on_this_page, authoritative_total) — total is parsed from
    the page's own "Results X – Y of Z" label, or None if it couldn't be
    found (treated as "stop paginating" by the caller).
    """
    params: dict = {"q": "", "locationsearch": "india"}
    if start:
        params["startrow"] = start

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("ANZ J2W: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ANZ search fetch failed: {exc}") from exc

    soup = BeautifulSoup(r.text, "html.parser")

    total = None
    m = _RESULTS_RE.search(r.text)
    if m:
        total = int(m.group(3))

    jobs: list[dict] = []
    for row in soup.select("tr.data-row"):
        link = row.select_one("span.jobTitle.hidden-phone a.jobTitle-link")
        if not link:
            link = row.select_one("a.jobTitle-link")
        if not link:
            continue

        href = link.get("href", "").strip()
        title = html_mod.unescape(link.get_text(strip=True))
        if not href or not title:
            continue

        # Job ID: trailing numeric segment of the path
        # e.g. "/job/Bengaluru-Software-Engineer/1366088966/" -> "1366088966"
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id.isdigit():
            continue

        loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
        if not loc_cell:
            loc_cell = row.select_one("span.jobLocation")
        loc_text = ""
        if loc_cell:
            for part in loc_cell.children:
                raw_part = getattr(part, "string", None) or (str(part) if hasattr(part, "strip") else "")
                candidate = raw_part.strip()
                if candidate and not candidate.startswith("+"):
                    loc_text = html_mod.unescape(candidate)
                    break

        # Normalise "Bengaluru, IN" -> "Bengaluru, India"
        loc_text = re.sub(r",\s*IN\b", ", India", loc_text)
        if not loc_text:
            loc_text = "India"

        date_span = row.select_one("td.colDate.hidden-phone span.jobDate")
        if not date_span:
            date_span = row.select_one("span.jobDate")
        posting_date = _parse_search_date(date_span.get_text(strip=True)) if date_span else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc_text,
            "posting_date": posting_date,
            "application_url": f"{_BASE_URL}{href}",
        })

    return jobs, total


def _fill_cache(timeout: int = 20) -> None:
    global _anz_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    start = 0
    total: int | None = None

    while True:
        page, page_total = _fetch_page(start, timeout)
        if total is None:
            total = page_total
        if not page:
            break
        for job in page:
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                collected.append(job)
        start += len(page)
        # Stop before `start` reaches/exceeds the authoritative India total —
        # requesting startrow past it silently drops the location filter and
        # returns unrelated global jobs instead of an empty page (see module
        # docstring). A missing/zero total is treated as "stop now" too.
        if not total or start >= total:
            break
        time.sleep(0.2)

    _anz_cache = collected
    print(f"[ANZ] Cache filled: {len(collected)} India jobs")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of ANZ India jobs.

    Keywords are ignored — ANZ's `q` search parameter is a loose/unreliable
    pre-filter (confirmed live: a nonsense query still returned several
    results); the shared title/skill filters in matcher.py do the real
    work. The full India pool is fetched once per process and cached.
    """
    _fill_cache(timeout=timeout)
    return _anz_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single ANZ job."""
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"ANZ description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    if r is None:
        return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    desc_span = soup.select_one("span.jobdescription")
    description = ""
    if desc_span:
        raw = html_mod.unescape(desc_span.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    date_meta = soup.find("meta", {"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_detail_date(date_meta.get("content", ""))

    return description, posting_date
