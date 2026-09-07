"""Fetches Air India job listings via the SAP SuccessFactors J2W "tile" theme
career site (careers.airindia.com).

ATS discovery (live, 2026-09-07): careers.airindia.com is the airline's own
SuccessFactors career site (CSP headers cite career44.sapsf.com/hcm44.sapsf.com,
matching Birlasoft/Nomura/Dover's own J2W tenants elsewhere in this repo), but
running the newer "tile" theme (`#job-tile-list` / `.job-tile-cell` divs) —
NOT the classic `<tr class="data-row">` table theme those tenants use. Search
endpoint: `GET /search/?q=<kw>`.

- `locationsearch=<value>` is NOT a reliable India filter on this tenant —
  confirmed live: `locationsearch=India` alone returns 0 tiles, while
  `locationsearch=Gurugram` returns 19 (and combining `q` + `locationsearch`
  ANDs them into a single Lucene-style query, e.g. `q=engineer&
  locationsearch=India` renders "Search results for 'engineer AND India'"
  and returns 0 -- both params together are worse than either alone). This
  differs from Birlasoft's tenant, where `locationsearch=India` is a clean,
  reliable filter — a genuinely new per-tenant behavior, not assumed from
  precedent. Not sent here at all as a result.
- The observed board has 28 postings in recognized Indian cities and one
  location labeled only "HO". Normalize known city names to include India;
  leave ambiguous/unknown locations unchanged so the shared location filter
  cannot mistake a future overseas posting for an Indian one.
- `q` (keyword) does narrow server-side (a nonsense token
  `zzznonsensequeryabc123` returns 0 tiles — not silently ignored), but the
  matching is loose/OR-based across tokens on a board this small: e.g.
  "senior software engineer" and "C# developer" each return 17-18 of the
  29 total postings (same over-inclusive-not-under-inclusive behavior as
  RippleHire tenants elsewhere in this repo — "developer"/"engineer" alone
  pull in most of the board). Given the whole board is only 29 postings,
  this fetcher ignores `q` entirely and caches the full board once per
  process (same "cache-once, let matcher.py do the real narrowing"
  reasoning as Birlasoft/UBS/PepsiCo), rather than re-paginating a
  29-job board once per keyword in the default 10-keyword list.
- Pagination: `startrow=N`, fixed 25/page, terminates cleanly with an empty
  `.job-tile-cell` result set past the true total (confirmed at
  startrow=25 on a 29-job pool: 4 remaining tiles, no wraparound).

Tile HTML repeats the same title/location three times per posting (desktop/
tablet/mobile responsive variants of the same tile) — the tablet variant's
`div[id$="-tablet-section-location-value"]` is used here because it's the
only one of the three that reliably includes the region suffix (e.g.
"Mumbai, Western", "Gurugram, HO") even for jobs whose desktop variant omits
a city value entirely (e.g. "Senior Associate - CC Productivity" -> desktop
value is empty, tablet value is the literal "HO").

Job IDs are the trailing numeric segment of the detail-page path (e.g.
`/job/Gurugram-Engineer-Backend/59188944/` -> "59188944").

Detail pages use the exact same selectors as Birlasoft's classic-theme
tenant: `<span class="jobdescription">` for the full JD and
`<meta itemprop="datePosted" content="Tue Aug 25 00:00:00 UTC 2026">` for the
posting date — confirmed live on a real "Engineer - Backend" posting.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.airindia.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # J2W tile theme returns 25 per page; not configurable
_MAX_PAGES = 20  # safety ceiling comfortably above the current ~29-job pool

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_JOB_ID_RE = re.compile(r"/(\d+)/?$")

# Module-level cache: filled once, reused for every keyword call in this
# process (keywords are ignored -- see module docstring).
_job_cache: list[dict] = []
_cache_filled: bool = False
_cache_error: RateLimitError | None = None

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Air India's J2W site."""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Tue Aug 25 00:00:00 UTC 2026' (meta tag) -> '2026-08-25'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalise_location(raw: str) -> str:
    """Add India only for a recognized city; preserve unknown/overseas values."""
    loc = " ".join((raw or "").split())
    if re.search(r"\bindia\b", loc, re.IGNORECASE):
        return loc
    city = loc.split(",", 1)[0].strip().casefold()
    india_cities = {
        "mumbai", "bengaluru", "bangalore", "gurugram", "gurgaon", "delhi",
        "new delhi", "amravati", "port blair", "vijayawada", "leh",
        "hyderabad", "chennai", "pune", "kolkata", "kochi", "ahmedabad",
    }
    return f"{loc}, India" if city in india_cities else loc


def _fetch_page(start: int, timeout: int) -> str:
    params = {"q": ""}
    if start:
        params["startrow"] = start

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Air India: 429 rate-limited during cache fill")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Air India cache fill failed: {exc}") from exc
    raise RateLimitError(f"Air India cache fill: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through the whole board once and cache every job.

    ``_cache_filled`` is set before the loop so a mid-fetch failure doesn't
    trigger a retry storm on every subsequent keyword call (the Honeywell/
    Persistent lesson).
    """
    global _job_cache, _cache_filled, _FIRST_PAGE_IDS, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None

    for page_num in range(_MAX_PAGES):
        start = page_num * _PAGE_SIZE
        if page_num > 0:
            time.sleep(0.15)

        try:
            html_text = _fetch_page(start, timeout)
        except RateLimitError as exc:
            _cache_error = exc
            raise
        soup = BeautifulSoup(html_text, "html.parser")
        if soup.select_one("#job-tile-list") is None:
            _cache_error = RateLimitError("Air India: search results container missing")
            raise _cache_error
        tiles = soup.select(".job-tile-cell")
        if not tiles:
            break

        page_ids: set[str] = set()
        new_this_page = 0
        for tile in tiles:
            link = tile.select_one("a.jobTitle-link")
            if not link:
                continue
            href = (link.get("href") or "").strip()
            title = html_mod.unescape(link.get_text(strip=True))
            if not href or not title:
                continue

            m = _JOB_ID_RE.search(href)
            job_id = m.group(1) if m else ""
            if not job_id:
                continue
            page_ids.add(job_id)
            if job_id in seen_ids:
                continue

            loc_div = tile.select_one('div[id$="-tablet-section-location-value"]')
            loc_text = html_mod.unescape(loc_div.get_text(strip=True)) if loc_div else ""
            location = _normalise_location(loc_text)

            seen_ids.add(job_id)
            new_this_page += 1
            collected.append({
                "id": job_id,
                "title": title,
                "location": location,
                "posting_date": "",  # not present in search results; filled on detail fetch
                "application_url": urljoin(_BASE_URL, href),
            })

        if page_num == 0:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1

        if new_this_page == 0:
            break

    _job_cache = collected
    print(f"[Air India] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Air India jobs.

    Keywords are ignored (see module docstring) -- the whole board (~29
    postings) is cached once per process and matcher.py's shared
    title/skill filters do the real narrowing, even though `q` does
    genuinely (if loosely) filter server-side.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description and posting date from a detail page.

    Returns (description_text, posting_date) where posting_date is
    YYYY-MM-DD. The detail page uses <span class="jobdescription"> and
    <meta itemprop="datePosted" content="Tue Aug 25 00:00:00 UTC 2026">
    (same selectors as Birlasoft's classic-theme J2W tenant).
    """
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Air India description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Air India description fetch failed: {exc}") from exc

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
