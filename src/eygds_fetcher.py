"""Fetches EY GDS (EY Global Delivery Services, India) job listings via
SAP SuccessFactors Job2Web (J2W), classic server-rendered theme.

`careers.ey.com` is the same SuccessFactors J2W classic platform as
deloitteusi_fetcher.py in this same onboarding batch (`<tr class="data-row">`
server-rendered HTML, identical `BS3ColumnizedSearch` CSS signature), but EY
has no separate India-only board node the way Deloitte's South Asia member
firm does -- `careers.ey.com/ey/search/` is a single GLOBAL board (7,976
jobs worldwide). India is scoped via the `locationsearch=India` free-text
location parameter (confirmed to be a genuine location-field filter, not a
generic keyword match: a sample of results all carry a real `IN` country
code and Indian postal code, e.g. "Hyderabad, IN, 500032") -- this narrows
the pool to 2,342 India postings, confirmed via the page's own "Results 1-25
of 2342" label. Pagination is `?startrow=N` (query-string based, the
Capgemini variant of this ATS rather than Nomura's path-segment variant),
confirmed to terminate cleanly past the true total with no wraparound.

**Keyword search on this tenant is the same noisy full-text OR-match problem
as Deloitte USI's tenant, only worse** -- confirmed live with
`locationsearch=India` applied: ".NET developer" returns 2,285 of 2,342
total and "C# developer" returns 2,290 (both ~98% of the entire India pool),
which only makes sense if "developer" (present in essentially every posting,
plausibly via generic "professional development"/"career development" HR
boilerplate in the JD body) is being OR-matched rather than the two-word
phrase being required as a unit; a nonsense token still correctly returns 0.
Given this, the fetcher caches the full 2,342-job India pool ONCE
(`locationsearch=India`, empty `q`) and ignores the `keyword` argument
entirely, same approach and same reasoning as deloitteusi_fetcher.py.
`eygds` should be registered in `_IGNORES_KEYWORDS`.

Two things this tenant does differently from Deloitte's, both handled here:

1. **No date column in the listing at all** (Deloitte's has one) -- this
   tenant's `search-results-header` only has Title/Location columns. Rather
   than accept an empty `posting_date` for the entire 2,342-job cache (which
   would only ever get backfilled for the handful of jobs that pass every
   other filter and reach the description-fetch step in matcher.py),
   `posting_date` is left `""` at listing time by design; `fetch_job_
   description` supplies the real value from the detail page's own
   `itemprop="datePosted"` meta tag for any job that gets that far.

2. **Locations carry a real state facet the listing text itself exposes**
   (e.g. "Chennai, TN, IN, 600113", "New Delhi, National Capital Territory,
   IN, 110037") -- unlike Deloitte's bare city-only strings. Chennai/Kochi/
   Trivandrum already say their real city name and are caught by config's
   default `exclude_locations` directly, but a hypothetical Tamil Nadu city
   that only carries the state's two-letter code (e.g. a future "Coimbatore,
   TN, IN, ...") would NOT trip the default list's "Tamil Nadu" substring
   check. Same leak class as eurofins_fetcher.py's `region: "TN"` gap and
   deloitteusi_fetcher.py's bare-city gap in this same batch -- fixed the
   same way: the state segment is expanded from "TN" to "Tamil Nadu" before
   handing the location off to matcher.py.
"""

from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.ey.com"
_SEARCH_PATH = "/ey/search/"

_PAGE_SIZE = 25
_CACHE_HARD_CAP = 6000  # far above the ~2,400 known India total; safety valve

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
    """Raised on 429 / persistent connection failure from the EY
    SuccessFactors J2W site."""


def _parse_detail_date(raw: str) -> str:
    """'Wed Aug 12 00:00:00 UTC 2026' -> '2026-08-12'."""
    m = re.search(r"[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+UTC\s+(\d{4})", raw)
    if not m:
        return ""
    mon, day, year = m.groups()
    mon_num = _MONTHS.get(mon.lower(), "")
    if not mon_num:
        return ""
    return f"{year}-{mon_num}-{int(day):02d}"


def _build_location(raw_loc: str) -> str:
    """'Chennai, TN, IN, 600113' -> 'Chennai, Tamil Nadu, India'.

    Strips the trailing ', IN[, POSTAL]' country/postal suffix, expands a
    bare 'TN' state token to 'Tamil Nadu' (see module docstring), then
    appends ', India' so matcher.py's is_india_job() passes.
    """
    raw_loc = raw_loc.strip()
    m = re.match(r"^(.*?),\s*IN\s*(?:,\s*\d+)?\s*$", raw_loc)
    prefix = m.group(1).strip() if m else raw_loc

    tokens = [t.strip() for t in prefix.split(",") if t.strip()]
    if len(tokens) > 1 and tokens[1].upper() == "TN":
        tokens[1] = "Tamil Nadu"

    if not tokens:
        return "India"
    return ", ".join(tokens) + ", India"


def _fetch_page_html(offset: int, timeout: int) -> str:
    url = _BASE_URL + _SEARCH_PATH
    params = {"q": "", "locationsearch": "India", "startrow": offset}

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("EY GDS: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
    raise RateLimitError(f"EY GDS search failed: {last_exc}")


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
        loc = _build_location(raw_loc) if raw_loc else "India"

        app_url = _BASE_URL + href if href.startswith("/") else href

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            # No date column on this tenant's listing rows (unlike Deloitte's
            # sibling J2W tenant) -- backfilled from the detail page in
            # fetch_job_description for jobs that reach that stage.
            "posting_date": "",
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
    """Return a slice of EY GDS's full cached India job pool.

    `keyword` is intentionally ignored -- see module docstring; this
    tenant's search is a noisy full-text OR-match unsuitable for per-keyword
    server-side narrowing, so the entire India-scoped pool is cached once
    and matcher.py's own title/skill filters do the real precision work.
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
                raise RateLimitError(f"EY GDS description: 429 for {application_url}")
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
