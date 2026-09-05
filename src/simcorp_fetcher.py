"""Fetches SimCorp job listings via the Workday public REST API.

SimCorp's ATS is Workday, hosted at simcorp.wd3.myworkdayjobs.com, site
SimCorp_Jobs. Same CXS REST pattern as Wells Fargo/Genpact/etc — no
Playwright needed, confirmed live via a plain POST returning real JSON.

Key quirks:
- No usable location facet exists (`locationMainGroup`'s only value is a
  null-id placeholder, the same "Genpact" shape) — every posting is fetched
  globally and India is detected client-side.
- Like Accenture, the `total` field is only correct on the *first* page;
  every subsequent offset silently returns `total: 0` even though
  `jobPostings` still has real data. Pagination here terminates on an empty
  `jobPostings` array, never on `total`.
- Most genuine India postings show a plain city name with no "India"
  substring at all (`locationsText` is just "Noida" or "Hyderabad") — the
  location string returned to matcher.py is normalised to "<City>, India"
  so `is_india_job()`/`exclude_locations` work.
- **The interesting one**: a meaningful slice of SimCorp's real .NET/C#/
  Angular engineering roles (~15 of the ~71 real India postings) show up in
  the search listing as an ambiguous "2 Locations"/"3 Locations"/etc. label
  instead of a city name — Workday collapses multi-site postings this way
  whenever a single req is open in more than one office, and most of those
  combinations are NOT India (Warsaw+Milano, Bad Homburg+Vienna, Singapore+
  New York, ...). Naively skipping every "N Locations" posting (or naively
  keeping all of them) would either drop real India engineering roles or
  wrongly alert on e.g. a Germany+Austria req. The only reliable signal is
  each posting's own detail-page `country` field (`GET .../job/{path}` ->
  `jobPostingInfo.country.descriptor`), which is resolved once per ambiguous
  posting during the one-time cache fill. All confirmed India hits in this
  set resolve to a Noida+Hyderabad pair (never Chennai/Pune/other excluded
  cities), so the normalised location is built from whichever of
  `location`/`additionalLocations` matches a known India city.
- Full board is small (~273 jobs total, ~71 genuinely India) so the entire
  pool plus the ~48 ambiguous-location detail lookups are fetched once and
  cached in-module; keyword search genuinely narrows server-side
  (`searchText` full-text-matches the JD body) but is not used here since
  caching the whole pool once is simpler and already cheap.
- Like MUFG/UBS/Nvidia/Pfizer/Walmart, pagination wraps around past the true
  total instead of ever returning an empty page — offset 273 silently
  replays offset 0's exact 20 postings, forever. Confirmed live: an
  unguarded 40-page pagination loop collected 793 raw postings (273 unique,
  each repeated ~2-3x) before hitting the page cap. Fixed the same way as
  those other tenants: memoize page 1's first job id and stop as soon as it
  reappears. Without this guard the fetcher still produces *correct* results
  (matcher.py's own global id-dedup absorbs the duplicates) but silently
  does ~3x the real HTTP work every 30-minute cycle, including ~3x the
  ambiguous-location detail lookups above — caught by comparing this
  fetcher's own "N total" log line against a raw pagination probe, not by a
  visible failure.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://simcorp.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/simcorp/SimCorp_Jobs/jobs"
_JOB_BASE = f"{_BASE_URL}/SimCorp_Jobs"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/simcorp/SimCorp_Jobs"
_PAGE_SIZE = 20
_MAX_PAGES = 40  # safety ceiling (~800 jobs) — real pool is ~273

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_JOB_BASE}",
}

# Known SimCorp India office cities, as they appear verbatim in Workday's
# locationsText / jobPostingInfo.location fields (no "India" substring).
_INDIA_CITIES = {"noida", "hyderabad"}

# Module-level cache: the full India pool is resolved once per process and
# reused for every keyword/location call (this tenant has no location facet
# and every call would otherwise re-paginate + re-resolve ~270 jobs).
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper — Workday returns relative strings like "Posted 3 Days Ago"
# ---------------------------------------------------------------------------

def _parse_posted_on(posted_on: str) -> str:
    """Convert Workday's relative date string to YYYY-MM-DD."""
    if not posted_on:
        return ""
    s = posted_on.strip().lower()
    today = date.today()

    if "today" in s:
        return today.strftime("%Y-%m-%d")
    if "yesterday" in s:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if "30+" in s:
        return (today - timedelta(days=30)).strftime("%Y-%m-%d")

    m = re.search(r"(\d+)\s+day", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+week", s)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+month", s)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    return ""


def _job_id_from_posting(p: dict, external_path: str) -> str:
    for field in p.get("bulletFields", []):
        m = re.match(r"^(R-\d+)$", str(field).strip(), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    m = re.search(r"_(R-\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def _post_with_retries(url: str, body: dict, timeout: int, label: str):
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(url, headers=_HEADERS, json=body, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"SimCorp {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"SimCorp {label} failed: {exc}") from exc
    raise RateLimitError(f"SimCorp {label}: no response — {last_exc}")


def _get_with_retries(url: str, timeout: int, label: str):
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"SimCorp {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"SimCorp {label} failed: {exc}") from exc
    raise RateLimitError(f"SimCorp {label}: no response — {last_exc}")


def _resolve_ambiguous_location(external_path: str, timeout: int) -> str | None:
    """Resolve a "N Locations" posting's true country via its detail page.

    Returns a normalised "<City>, India" string if the posting is genuinely
    India-based, or None otherwise (including on a resolution failure —
    ambiguous postings are skipped rather than risking a false alert).
    """
    try:
        r = _get_with_retries(f"{_DETAIL_BASE}{external_path}", timeout, "location resolve")
    except RateLimitError:
        return None
    info = r.json().get("jobPostingInfo", {})
    country = (info.get("country") or {}).get("descriptor", "")
    if country.strip().lower() != "india":
        return None
    candidates = [info.get("location", "")] + list(info.get("additionalLocations", []) or [])
    for city in candidates:
        if city and city.strip().lower() in _INDIA_CITIES:
            return f"{city.strip()}, India"
    # Country says India but no recognised city — still a real India job.
    return "India"


def _fill_cache(timeout: int = 20) -> None:
    """Paginate the full SimCorp board once and cache genuine India postings.

    _cache_filled is set before the loop so a mid-run failure doesn't cause a
    retry storm on every subsequent keyword call (Honeywell/Persistent
    lesson) — a scan cycle that hits an error here simply yields no SimCorp
    jobs this cycle and self-heals on the next 30-minute run.
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    # Like MUFG/UBS/Nvidia/Pfizer/Walmart, this tenant's pagination wraps
    # around past the true total instead of returning an empty page (verified
    # live: offset 273 silently replays offset 0's exact 20 postings, forever
    # — the `total` field is useless here too, see the module docstring).
    # Memoize page 1's first job id and stop as soon as it reappears.
    all_postings: list[dict] = []
    offset = 0
    first_page_anchor: str | None = None
    for page_num in range(_MAX_PAGES):
        if page_num > 0:
            time.sleep(0.15)
        body = {"limit": _PAGE_SIZE, "offset": offset, "searchText": ""}
        r = _post_with_retries(_SEARCH_URL, body, timeout, "search")
        postings = r.json().get("jobPostings", [])
        if not postings:
            break
        page_anchor = _job_id_from_posting(postings[0], postings[0].get("externalPath", ""))
        if page_num == 0:
            first_page_anchor = page_anchor
        elif page_anchor and page_anchor == first_page_anchor:
            break  # wrapped around to page 1
        all_postings.extend(postings)
        offset += len(postings)

    seen_job_ids: set[str] = set()
    collected: list[dict] = []
    for p in all_postings:
        external_path = p.get("externalPath", "")
        job_id = _job_id_from_posting(p, external_path)
        title = (p.get("title") or "").strip()
        if not (job_id and title and external_path):
            continue
        if job_id in seen_job_ids:
            continue
        seen_job_ids.add(job_id)

        loc_text = (p.get("locationsText") or "").strip()
        location: str | None
        if loc_text.lower() in _INDIA_CITIES:
            location = f"{loc_text}, India"
        elif "location" in loc_text.lower() and any(ch.isdigit() for ch in loc_text):
            # Ambiguous "N Locations" label — resolve via detail page.
            time.sleep(0.1)
            location = _resolve_ambiguous_location(external_path, timeout)
        else:
            location = None  # a known non-India single city — skip, no request needed

        if not location:
            continue

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": f"{_JOB_BASE}{external_path}",
        })

    _india_cache = collected
    print(f"[SimCorp] Cache filled: {len(collected)} India jobs (of {len(all_postings)} total)")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of SimCorp India jobs.

    Keywords are ignored: the Workday tenant's own `searchText` genuinely
    full-text-matches the JD body, but there is no location facet at all, so
    every ambiguous "N Locations" posting still needs a one-off detail
    lookup regardless of keyword — caching the whole ~273-job pool once and
    slicing it here is simpler than re-resolving locations per keyword.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/SimCorp_Jobs/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = _get_with_retries(api_url, timeout, "description")
    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""
    return description, posting_date
