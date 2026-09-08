"""Fetches Sapiens International job listings via SAP SuccessFactors J2W
(classic Job-to-Work) HTML scraping.

careers.sapiens.com is a classic (non-Unify) J2W tenant: `/search` returns
server-rendered `<tr class="data-row">` rows with no JS required, matching
the playbook's documented classic-J2W pattern. Found by loading the public
careers page and following the SuccessFactors script/link references
(career5.successfactors.eu backing services, `/platform/js/j2w/...`
assets) -- no `wd1.myworkdayjobs.com` or other third-party ATS host is
used anywhere on the site.

Search takes plain query-string GET params (no CSRF token, no session
priming needed -- confirmed a completely cookie-less request returns the
same result set as a primed one):
  - `title`      -- free-text title filter, genuinely narrows server-side
                     (confirmed: a nonsense token returns zero rows) --
                     BUT it does a literal-substring match against the
                     job title, not an OR-token match, so a multi-word
                     configured keyword like "software engineer" returns
                     zero rows against real titles like "Senior AI
                     Engineer" or "DevOps Architect" that never contain
                     that exact phrase. Registered with `_IGNORES_KEYWORDS`
                     and this fetcher ignores the incoming `keyword`
                     entirely (same "cache the whole India pool, let
                     matcher.py's own title-family/skill filters do the
                     real work" pattern as Persistent/Clearwater) rather
                     than risk silently missing real matches
  - `locationsearch` -- free-text location filter, genuinely narrows
                     server-side and is more precise than stuffing the
                     same term into `q` (confirmed live 2026-09-08:
                     `locationsearch=India` returns 22 India rows vs a
                     looser/noisier 24 for `q=India`, which also matched
                     some non-India rows through full-text search)
  - `startrow`   -- row-offset pagination (NOT page-number -- confirmed
                     `startrow=15` on a 24-row result set returns rows
                     15-23, not "page 2 of 15-per-page")

The site's own fixed page size is 15 rows per request, which does not
divide evenly into matcher.py's `_PAGE_SIZE = 20` stepping (start=0, 20,
40, ...). Naively forwarding `start` straight through as `startrow` would
silently skip rows 15-19 whenever a caller asks for `num=20` starting at
`start=0` (the site would return only rows 0-14, then the next matcher
call jumps to `start=20`, hard-skipping 5 real rows in between).
`fetch_jobs` instead loops internally over the site's own row-sized
sub-pages until it has assembled up to `num` contiguous rows starting
exactly at `start`, so no row is ever skipped regardless of the page-size
mismatch.

This is a genuinely small tenant: 78 total live reqs / ~22 in India (all
Bangalore -- confirmed live 2026-09-08, no Chennai/Pune/Tamil
Nadu/Chandigarh/Kochi postings currently exist on this tenant), so the
extra sub-page requests are cheap.

Detail pages are plain server-rendered HTML: description lives in
`<span class="jobdescription">` and the posting date in a
`<meta itemprop="datePosted" content="...">` tag -- exactly the classic
J2W shape documented in the playbook.
"""

from __future__ import annotations

import html
import re
import time
import warnings
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

_BASE = "https://careers.sapiens.com"
_SEARCH_URL = f"{_BASE}/search"

_SITE_PAGE_SIZE = 15

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{_BASE}/",
}

_ROW_RE = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
_LINK_RE = re.compile(r'href="([^"]+)"[^>]*class="jobTitle-link"[^>]*>([^<]+)</a>')
_LOC_RE = re.compile(r'jobLocation">\s*([^<]+?)\s*</span>')

# The site renders location as "City, XX" using a 2-letter ISO country code
# (e.g. "Bangalore, IN"), not the word "India" -- matcher.py's Layer 1
# check is a literal `"india" in location.lower()` substring test, so a
# bare ", IN" suffix would silently fail every job. Normalize the trailing
# country-code token to the full country name.
_COUNTRY_CODE_RE = re.compile(r",\s*IN$", re.IGNORECASE)


def _normalize_location(loc: str) -> str:
    return _COUNTRY_CODE_RE.sub(", India", loc)
_ID_RE = re.compile(r"/job/[^/]+/(\d+)/?$")

_DESC_RE = re.compile(r'class="jobdescription">(.*?)</span>\s*</div>', re.S)
_DATE_RE = re.compile(r'itemprop="datePosted"\s+content="([^"]+)"')


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from careers.sapiens.com."""


def _get(url: str, params: dict, timeout: int, error_label: str) -> requests.Response:
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    url, params=params, headers=_HEADERS, timeout=timeout, verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"{error_label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"{error_label} fetch failed: {exc}") from exc
    return r


def _parse_posted_date(raw: str) -> str:
    """Convert 'Mon Aug 31 00:00:00 UTC 2026' style dates to YYYY-MM-DD."""
    if not raw:
        return ""
    for fmt in ("%a %b %d %H:%M:%S %Z %Y", "%a %b %d %H:%M:%S UTC %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _scrape_rows(location: str, startrow: int, timeout: int) -> list[dict]:
    """Fetch a single site-sized (<=15 row) page starting at `startrow`.

    `title` is intentionally left blank -- see the `_IGNORES_KEYWORDS`
    rationale in the module docstring; the incoming `keyword` argument to
    `fetch_jobs` is not used server-side at all.
    """
    params = {"title": "", "q": "", "startrow": startrow}
    if location:
        params["locationsearch"] = location

    r = _get(_SEARCH_URL, params, timeout, "Sapiens search")
    page_html = r.text

    rows: list[dict] = []
    for row_html in _ROW_RE.findall(page_html):
        link_m = _LINK_RE.search(row_html)
        if not link_m:
            continue
        href, title = link_m.group(1), html.unescape(link_m.group(2).strip())
        if not title:
            continue

        id_m = _ID_RE.search(href)
        if not id_m:
            continue
        job_id = id_m.group(1)

        loc_m = _LOC_RE.search(row_html)
        loc = html.unescape(loc_m.group(1).strip()) if loc_m else ""
        loc = _normalize_location(loc)

        rows.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": "",
            "application_url": urljoin(_BASE, href),
        })
    return rows


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    collected: list[dict] = []
    row = start
    # Loop over the site's own (smaller) page size until `num` rows are
    # assembled or the pool is exhausted, so matcher.py's fixed 20-row
    # stepping never skips a row.
    while len(collected) < num:
        batch = _scrape_rows(location, row, timeout)
        if not batch:
            break
        collected.extend(batch)
        row += len(batch)
        if len(batch) < _SITE_PAGE_SIZE:
            # Short page: this was the last page of results.
            break

    return collected[:num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    if not application_url:
        return "", ""

    r = _get(application_url, {}, timeout, "Sapiens description")
    page_html = r.text

    desc_m = _DESC_RE.search(page_html)
    description = ""
    if desc_m:
        description = " ".join(
            BeautifulSoup(desc_m.group(1), "html.parser").get_text(separator=" ").split()
        )

    date_m = _DATE_RE.search(page_html)
    posting_date = _parse_posted_date(date_m.group(1)) if date_m else ""

    return description, posting_date
