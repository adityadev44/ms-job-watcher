"""Fetches Iris Software (careers.irissoftware.com) India job listings via
the SAP SuccessFactors J2W HTML search API -- the classic (non-Unify) J2W
platform, same product family as Nomura/Capgemini/SAP Labs/Mastek. View
source on `/search/` shows real server-rendered `<li class="job-tile">`
tiles (the "tile" theme skin, same as Mastek -- not the `<tr class=
"data-row">` rows used by Capgemini/SAP Labs, and NOT the Unify REST-API
theme used by Standard Chartered/Wipro/HCLTech). Confirmed live 2026-09-04
via `j2w.init(...)` on the page (`ssoCompanyId: 'irissoftwa'`,
`categoryId: 565044`).

IMPORTANT dead end avoided: the page also embeds a `X-CSRF-Token` in a
`$.ajaxSetup(...)` block, which looks identical to the Unify pattern
(Standard Chartered/Wipro/HCLTech) at first glance. Following that pattern
and POSTing to `/services/recruiting/v1/jobs` with a matching CSRF token +
session cookie consistently returns HTTP 401 `{"error":{"message":
"Error retrieving jobs"}}` -- for every category ID, with a freshly-fetched
token every time. A live Playwright network capture of a real browser
session proved that endpoint is simply never called by this tenant's
frontend at all -- the real job list is served by a plain, session-less,
unauthenticated GET to `/search/` (first page) and `/tile-search-results/`
(subsequent pages). Lesson: a CSRF token's *presence* on a J2W page does not
mean the Unify REST API is the one actually in use -- verify via a real
network capture, not by pattern-matching the page's own JS config.

`/search/` accepts `searchType=linkquery`, `q=<keyword>` (the real param
name -- NOT `keywords`, which is silently a no-op since no such field
exists), `locationsearch=`, and `category=<id>`. `q` genuinely filters
server-side against full JD text (verified: `q=.NET` -> 31 of 340; a
nonsense string -> a clean empty result with no "Showing ..." header at
all -- NOT the Mastek/TCS-style silent fallback-to-default-pool bug). The
`category` param has no observable effect either way (IT-Jobs/565044/no
category all return the identical 340-job pool) -- this tenant's whole
candidate pool appears to already be India-scoped. Since the full pool is
small and the fetcher caches it once regardless, keywords are intentionally
ignored here and the shared matcher does the real title/skill narrowing
(same pattern as Mastek/CRED/Groww/Persistent).

Pagination is `startrow=N` (30/page, `data-per-page="30"`), read via the
first page's "Showing 1 to 30 of 340 Jobs" header. Confirmed NOT to wrap
around: `startrow` at/past the true total cleanly returns zero tiles (no
UBS/MUFG/Nvidia/Pfizer/Walmart/Mastek-style bleed-in of unrelated jobs).

`sortColumn=referencedate&sortDirection=desc` is sent on every page,
including the first -- without it, the *first* `/search/` page returns an
unsorted/arbitrary order while later `/tile-search-results/` pages default
to no sort too unless asked, and a real, currently-open, on-target job can
land almost anywhere in that order (confirmed live: the flagged ".Net Core
- Senior Engineer" role sat at position 339 of 340 in the unsorted order).
Since `find_matching_jobs()` in matcher.py caps each keyword/location combo
at `max_listings` before ever looking at cache order, an unsorted cache
combined with a `max_listings` set too close to the true total will
silently truncate real matches -- see the `irissoftware_search` block in
config.yaml, which sets `max_listings: 400` (comfortably above the ~340
known total) for exactly this reason, independent of this sort fix.

Each `<li class="job-tile job-id-{id}">` embeds 3 responsive-layout
duplicates (desktop/tablet/mobile) of the same title/location/date fields --
`select_one` picking the first match is intentional, same approach as
Mastek. Location text already reads "Noida, UP, India" / "Gurugram, HR,
India" verbatim -- no IN->India normalisation needed (unlike Mastek/
Capgemini), and no other India city has appeared in this tenant's pool.

List-tile posting date uses a British "D Mon YYYY" format where September
renders as the 4-letter "Sept" (not the standard 3-letter "%b" abbreviation
strptime expects for other months) -- e.g. "4 Sept 2026" vs "7 Aug 2026".
Parsed via an explicit month-name table rather than strptime. The detail
page's `meta[itemprop="datePosted"]` uses the unambiguous
"Fri Aug 07 00:00:00 UTC 2026" format (same anchor as Mastek/SAP Labs) and
is used as the authoritative date; the list-tile date is just a fallback
for the interim between fetch_jobs() and the detail fetch.

Titles are level-banded IT-services style ("<Tech> - Senior Engineer",
"<Tech> - Lead", "<Tech> - Associate Manager") but -- unlike Wipro/HCLTech/
DXC/Mastek -- the tech stack is named directly IN the title itself (".Net
Core - Senior Engineer", "Java Fullstack - Lead", "Gen AI - Senior
Engineer", "QA Automation C# - Senior Engineer", ...), same shape as NEC
Software Solutions. `require_tech_in_description` is deliberately NOT
enabled for this reason -- every title that passes the shared
`title_family`/`exclude_terms` check already names its own stack, so the
false-positive risk Layer 4 exists for (a generic title hiding an unrelated
stack) is structurally much smaller here than at Wipro/HCLTech.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.irissoftware.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_TILE_RESULTS_URL = f"{_BASE_URL}/tile-search-results/"
_CATEGORY_ID = 565044  # "All-Job-Openings" -- j2w.init()'s categoryId on the browse page
_PAGE_SIZE = 30  # fixed by this J2W tile theme; not configurable via query params

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Iris's J2W tenant."""


# Module-level cache: the full pool is small (~340 jobs) and keywords aren't
# honoured by this fetcher (see module docstring), so it's fetched once and
# fetch_jobs()/fetch_job_description() are served from it afterward.
_job_cache: list[dict] = []
_cache_filled: bool = False


# ---------------------------------------------------------------------------
# Date / location helpers
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert '4 Sept 2026' / '7 Aug 2026' (search tile) to '2026-09-04'.

    Uses an explicit month table instead of strptime's %b because this
    tenant's en_GB locale renders September as the 4-letter "Sept", which
    %b (expecting the standard 3-letter "Sep") fails to parse.
    """
    if not raw:
        return ""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", raw.strip())
    if not m:
        return ""
    day, mon, year = m.groups()
    month = _MONTHS.get(mon.lower())
    if not month:
        return ""
    try:
        return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Fri Aug 07 00:00:00 UTC 2026' (meta tag) to '2026-08-07'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Cache fill
# ---------------------------------------------------------------------------

def _fetch_page(start: int, timeout: int) -> str:
    """GET one page of tiles; 3-attempt retry with exponential backoff."""
    if start == 0:
        url = _SEARCH_URL
        params = {
            "searchType": "linkquery",
            "q": "",
            "locationsearch": "",
            "category": _CATEGORY_ID,
            "sortColumn": "referencedate",
            "sortDirection": "desc",
        }
    else:
        url = _TILE_RESULTS_URL
        params = {
            "q": "",
            "sortColumn": "referencedate",
            "sortDirection": "desc",
            "startrow": start,
        }

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Iris Software J2W: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Iris Software search fetch failed: {exc}") from exc

    raise RateLimitError(f"Iris Software search fetch: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire India job pool once; cache job list.

    _cache_filled is set before the request attempts so a transient failure
    doesn't trigger a retry storm on every fetch_jobs() call made during the
    same process run (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    total: int | None = None
    start = 0

    while True:
        html_text = _fetch_page(start, timeout)

        if total is None:
            m = re.search(r"Showing \d+ to \d+ of (\d+) Jobs", html_text)
            if not m:
                m = re.search(r"Showing (\d+) Job\b", html_text)
            if m:
                total = int(m.group(1))

        soup = BeautifulSoup(html_text, "html.parser")
        tiles = soup.select("li.job-tile")
        if not tiles:
            break

        for li in tiles:
            data_url = (li.get("data-url") or "").strip()
            job_id = data_url.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit() or job_id in seen_ids:
                continue

            link = li.select_one("a.jobTitle-link")
            title = html_mod.unescape(link.get_text(strip=True)) if link else ""
            if not title:
                continue

            loc_div = li.select_one('div[id*="-location-value"]')
            loc_text = (loc_div.get_text(strip=True) if loc_div else "") or "India"

            date_div = li.select_one('div[id*="-date-value"]')
            posting_date = (
                _parse_search_date(date_div.get_text(strip=True)) if date_div else ""
            )

            seen_ids.add(job_id)
            jobs.append({
                "id": job_id,
                "title": title,
                "location": loc_text,
                "posting_date": posting_date,
                "application_url": f"{_BASE_URL}{data_url}",
            })

        start += _PAGE_SIZE
        # Stop once the known total is covered, or after a generous safety
        # cap -- never issue a request at/past the true total (confirmed
        # clean zero-tile termination for this tenant, but stay defensive
        # the way every other cache-once fetcher in this repo does).
        if total is not None and len(seen_ids) >= total:
            break
        if start > 2000:
            break
        time.sleep(0.2)

    _job_cache[:] = jobs
    print(f"[Iris Software] Cache filled: {len(jobs)} India jobs")


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
    """Return a page of Iris Software India jobs.

    keyword/location are accepted but ignored -- the full pool is small
    (~340 jobs) and is cached once per process; the shared matcher does the
    real title/skill filtering. (Iris's own `q=` search param does genuinely
    filter server-side, unlike several other companies' broken keyword
    handling, but there's no need to use it here.)
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Iris Software job.

    Detail page uses <span class="jobdescription"> (single occurrence,
    unlike Wipro/HCLTech's duplicate-itemprop trap) and
    <meta itemprop="datePosted" content="Fri Aug 07 00:00:00 UTC 2026">.
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Iris Software description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Iris Software description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Iris Software description fetch: no response -- {last_exc}")

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
