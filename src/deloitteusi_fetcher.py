"""Fetches Deloitte USI (Deloitte India / South Asia) job listings via
SAP SuccessFactors Job2Web (J2W), classic server-rendered theme.

kpmg-style secondary research pointed at `apply.deloitte.com`/
`jobsearch.deloitte.com`, but those turned out to be a US-only Oracle Taleo
career section and a dead domain respectively -- neither is India-relevant.
The real India/South Asia board is linked from `www2.deloitte.com/in/en/
careers.html` as "southasiacareers.deloitte.com", a classic-theme
SuccessFactors J2W site (`<tr class="data-row">` server-rendered HTML,
`BS3ColumnizedSearch` CSS -- same platform family as nomura_fetcher.py/
capgemini_fetcher.py in this repo). The India-only board node is
`/go/Deloitte-India/718244/` (found directly in the page's own links,
alongside sibling `/go/Deloitte-Mauritius/...` and `/go/Deloitte-Sri-Lanka-
and-Maldives/...` nodes for the same South Asia member firm) -- confirmed
1,678 total postings, spot-checked across five widely-spaced pages with
every single `locationsText` ending in ", IN" (no non-India leakage
observed).

**Keyword search on this tenant is a noisy full-text OR-match, not reliable
per-word AND filtering** -- confirmed live: "software engineer" returns 1,251
of 1,678 total, and adding a THIRD word ("senior software engineer") returns
MORE matches (1,395), not fewer, which is inconsistent with real phrase/AND
narrowing and is only explainable by an OR-across-tokens combiner scored
across a broader search corpus than just the visible title (plausibly the
full JD body, where common words like "engineer"/"developer"/"python" appear
in unrelated HR boilerplate). Only two-word niche terms narrow usefully in
isolation (".NET" -> 35, "angular" -> 46), but the shared default keyword
list is mostly multi-word phrases that would each fetch most of the whole
board. Given this, the fetcher caches the FULL India pool ONCE per process
(paginated via the site's own 25-per-page path-offset scheme, e.g.
`/go/Deloitte-India/718244/1675/?q=&sortColumn=referencedate&sortDirection=
desc`, confirmed to terminate cleanly with an empty page past the true
total -- no wraparound) and ignores the `keyword` argument entirely,
returning local slices of the cached pool. `deloitteusi` should be
registered in `_IGNORES_KEYWORDS` so the runner issues one query pass
instead of ten near-identical ones.

Locations are city-only with a bare ", IN" suffix (e.g. "Bengaluru, IN"),
never the literal word "India" -- rewritten to ", India" so matcher.py's
`is_india_job()` passes. A live sample turned up 3 Coimbatore postings,
a genuine Tamil Nadu city whose name alone never trips config's default
`exclude_locations` (which only matches "Tamil Nadu"/"Chennai"/"Madurai"
literally) -- same leak class as eurofins_fetcher.py's Coimbatore/Tiruppur
gap. Fixed the same way: a small known-Tamil-Nadu-city list appends
", Tamil Nadu" before ", India" so the existing default exclusion catches
it without any config/matcher.py change.

Posting dates ARE shown in the listing itself (a `jobDate` column, e.g.
"Sep 6, 2026") -- unlike EY GDS's tenant in this same onboarding batch --
so no per-job detail fetch is needed just to sort/cache correctly.
Descriptions are fetched from the job detail HTML page's own
`itemprop="description"` span (present exactly once per page, no
Wipro/HCLTech-style duplicate-boilerplate risk observed), with the posting
date cross-checked from `itemprop="datePosted"`.
"""

from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://southasiacareers.deloitte.com"
_BOARD_PATH = "/go/Deloitte-India/718244/"

_PAGE_SIZE = 25
_CACHE_HARD_CAP = 5000  # far above the ~1,700 known total; safety valve only

# Tamil Nadu cities that appear bare (e.g. "Coimbatore, IN") with no state
# name in the string at all -- config's default exclude_locations only
# matches "Tamil Nadu"/"Chennai"/"Madurai" literally, so these would
# otherwise leak through. Chennai/Madurai are already covered by name and
# are not repeated here.
_TN_CITIES = {
    "coimbatore", "tiruppur", "trichy", "tiruchirapalli", "salem",
    "erode", "vellore", "thanjavur", "tirunelveli", "hosur",
}

_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

_cache: list[dict] | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from the Deloitte
    SuccessFactors J2W site."""


def _parse_listing_date(raw: str) -> str:
    """'Sep 6, 2026' -> '2026-09-06'."""
    m = re.match(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),\s*(\d{4})", raw.strip())
    if not m:
        return ""
    mon, day, year = m.groups()
    mon_num = _MONTHS.get(mon.lower(), "")
    if not mon_num:
        return ""
    return f"{year}-{mon_num}-{int(day):02d}"


def _parse_detail_date(raw: str) -> str:
    """'Sun Sep 06 02:00:00 UTC 2026' -> '2026-09-06'."""
    m = re.search(r"[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+UTC\s+(\d{4})", raw)
    if not m:
        return ""
    mon, day, year = m.groups()
    mon_num = _MONTHS.get(mon.lower(), "")
    if not mon_num:
        return ""
    return f"{year}-{mon_num}-{int(day):02d}"


def _fetch_page_html(offset: int, timeout: int) -> str:
    url = _BASE_URL + _BOARD_PATH if offset == 0 else f"{_BASE_URL}{_BOARD_PATH}{offset}/"
    params = {"q": "", "sortColumn": "referencedate", "sortDirection": "desc"}

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Deloitte USI: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
    raise RateLimitError(f"Deloitte USI search failed: {last_exc}")


def _parse_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    for row in soup.select("tr.data-row"):
        a = row.select_one("td.colTitle a.jobTitle-link")
        if not a:
            continue
        href = a.get("href", "")
        title = a.get_text(strip=True)
        m = re.search(r"/job/[^/]+/(\d+)/?", href)
        if not m or not title:
            continue
        job_id = m.group(1)

        loc_el = row.select_one("td.colLocation.hidden-phone span.jobLocation")
        raw_loc = loc_el.get_text(strip=True) if loc_el else ""
        city = raw_loc.split(",")[0].strip()
        if city.lower() in _TN_CITIES:
            loc = f"{city}, Tamil Nadu, India"
        elif city:
            loc = f"{city}, India"
        else:
            loc = "India"

        date_el = row.select_one("td.colDate.hidden-phone span.jobDate")
        raw_date = date_el.get_text(strip=True) if date_el else ""

        app_url = _BASE_URL + href if href.startswith("/") else href

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_listing_date(raw_date),
            "application_url": app_url,
        })
    return jobs


def _fill_cache(timeout: int) -> list[dict]:
    global _cache
    if _cache is not None:
        return _cache

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None
    offset = 0

    while offset <= _CACHE_HARD_CAP:
        html = _fetch_page_html(offset, timeout)
        rows = _parse_rows(html)
        if not rows:
            break

        row_ids = {j["id"] for j in rows}
        if offset == 0:
            first_page_ids = row_ids
        elif first_page_ids and row_ids == first_page_ids:
            break  # pagination wraparound guard

        new_count = 0
        for j in rows:
            if j["id"] not in seen_ids:
                seen_ids.add(j["id"])
                jobs.append(j)
                new_count += 1
        if new_count == 0:
            break

        offset += len(rows)
        time.sleep(0.2)

    _cache = jobs
    return _cache


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a slice of Deloitte USI's full cached India job pool.

    `keyword` is intentionally ignored -- see module docstring; this
    tenant's search is a noisy full-text OR-match unsuitable for per-keyword
    server-side narrowing, so the entire pool is cached once and matcher.py's
    own title/skill filters do the real precision work.
    """
    jobs = _fill_cache(timeout)
    return jobs[start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Deloitte USI description: 429 for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    if r is None:
        return "", ""

    soup = BeautifulSoup(r.text, "html.parser")
    desc_el = soup.find(attrs={"itemprop": "description"})
    description = " ".join(desc_el.get_text(separator=" ").split()) if desc_el else ""

    date_meta = soup.find("meta", attrs={"itemprop": "datePosted"})
    raw_date = date_meta.get("content", "") if date_meta else ""
    posting_date = _parse_detail_date(raw_date)

    return description, posting_date
