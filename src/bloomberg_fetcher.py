"""Fetches Bloomberg L.P. job listings via bloomberg.avature.net (Avature ATS).

ATS discovery: `careers.bloomberg.com` and `jobs.bloomberg.com` both 301-redirect
to `bloomberg.avature.net/careers` -- the public `www.bloomberg.com` domain
itself sits behind PerimeterX (403 on plain requests, `_pxhd` cookie), but the
actual careers portal is a separate, unprotected Avature tenant. Same ATS
family already proven working in this repo for Macquarie
(`macquarie_fetcher.py`) -- this fetcher follows that same plain-HTML-scraping
pattern, adapted for Bloomberg's slightly different card/detail markup.

ATS behavior (bloomberg.avature.net/careers, Avature):
- Search: GET
    https://bloomberg.avature.net/careers/SearchJobs/{urlencoded keyword}
        ?listFilterMode=1&jobRecordsPerPage=9&jobOffset={N}
  Plain server-rendered HTML -- no JS/Playwright needed, confirmed via curl
  with no cookies/session. `jobRecordsPerPage` is fixed at 9 by the site
  regardless of the value requested (identical quirk to Macquarie); pagination
  walks `jobOffset` in steps of 9.
- The free-text `search` box genuinely filters server-side (confirmed:
  "software engineer" -> 90 jobs, "engineer" -> 233, "data engineer" -> 111,
  distinct result sets per keyword) -- but it appears to match against
  location text too, not just title/description (a bare "Mumbai" search
  surfaced Sales/Support roles whose location field contains "Mumbai" with no
  "engineer" anywhere in the title). Treated as genuine server-side keyword
  filtering (not added to `_IGNORES_KEYWORDS` in company_registry.py) since
  different keywords produce meaningfully different pools.
- A `SearchJobsData` JSON endpoint is advertised in the page's own
  `dataUrl` meta (an AJAX partial for infinite scroll) but returns an empty
  whitespace-only body to a plain `requests`/`urllib` call even with matching
  Referer/X-Requested-With headers and session cookies from a prior GET --
  likely needs additional client-side state. Not used; the server-rendered
  HTML list already has everything needed, exactly like Macquarie.
- No location/country facet is usable via plain HTTP: the "Location" field
  (fieldId 1845) is an opaque AJAX autocomplete with numeric option IDs, the
  same dead end hit for Macquarie. Unlike Macquarie, this doesn't matter here
  -- Bloomberg's location text on both the search-results card and the detail
  page already contains "India" directly ("Pune, India",
  "Mumbai, Maharashtra, India"), so `is_india_job()` in matcher.py handles
  India detection with zero fetcher-side location massaging.
- Every job card's title link and the footer "Apply"/"Save" buttons all point
  at the same `/JobDetail/{slug}/{id}` URL -- the numeric id must be parsed
  from the title link specifically (`.article__header__text__title a`), not
  any `<a>` matching `JobDetail`, or the footer buttons' identical hrefs would
  just re-confirm the same id (harmless here, but the title link is the only
  one carrying the actual job title text).
- **No posting-date field is exposed anywhere** -- not on the search-results
  card, not on the detail page (checked the meta fields block, the full page
  text, and for a JobPosting JSON-LD block; none exist). Same situation as
  `ibm_fetcher.py`: `posting_date` is always returned as `""`.
- Detail-page slugs are cosmetic and can go stale: re-requesting a URL by
  reconstructing `{title-slug}/{id}` from an old id can land on a *different,
  currently-live* posting at that same numeric id with an unrelated slug in
  the URL bar (Avature apparently ignores the slug and resolves purely off
  the trailing id, and ids get recycled onto new postings over time). This
  fetcher never reconstructs URLs -- it always uses the exact
  `application_url` captured live from the search-results page, so this is
  only a hazard for anyone tempted to hand-construct a Bloomberg job URL from
  a bare id later.
- Detail page: three `<article class="article--details">` blocks per
  posting -- title-only, a compact "Location / Business Area / Ref #" meta
  block, and the actual "Description & Requirements" content. Order was
  consistent across every posting checked, but the description is selected
  by taking the *longest* block's text (`len() > 100` guard, the Honeywell
  lesson) rather than trusting a fixed index, in case some posting omits one
  of the other two blocks.

India engineering presence: Bloomberg's India engineering hiring is
concentrated in Pune (opened 2014, "major global engineering center" per its
own JD boilerplate) -- confirmed across a broad keyword sweep. Mumbai has
real Bloomberg postings too, but every one sampled was Sales/Data-Ops/Support,
not software engineering. Pune is globally excluded by this repo's
`exclude_locations` config, so 0 current matches is the expected, correct
result today -- same situation already documented for Icertis/BNY Mellon. Not
worked around, per instructions; the fetcher and filter pipeline are correct
and will pick up genuine non-Pune India engineering postings the moment
Bloomberg opens any.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_BASE = "https://bloomberg.avature.net/careers"
_SEARCH_BASE = f"{_BASE}/SearchJobs"

_SITE_PAGE_SIZE = 9  # fixed by the site; jobRecordsPerPage is not honoured above this
_MAX_PAGES_PER_KEYWORD = 80  # defensive cap (~720 raw jobs) against runaway pagination

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Bloomberg's site."""


# Module-level cache: filled once per keyword, reused across matcher.py's
# repeated (start, num) page calls for that same keyword -- same pattern as
# macquarie_fetcher.py, since the search endpoint genuinely filters server-side
# and re-paginating from scratch on every call would be wasteful.
_cache: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# HTTP helper with retry/backoff
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Bloomberg: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Bloomberg request failed: {exc}") from exc
    raise RateLimitError(f"Bloomberg request failed: {last_exc}")


# ---------------------------------------------------------------------------
# Search-results page parsing
# ---------------------------------------------------------------------------

def _parse_search_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []

    for article in soup.find_all("article", class_="article--result"):
        link = article.select_one(".article__header__text__title a.link")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not (title and href):
            continue

        m = re.search(r"/JobDetail/[^/]+/(\d+)", href)
        job_id = m.group(1) if m else ""
        if not job_id:
            continue

        loc_el = article.select_one(".article__header__text__subtitle .list-item-location")
        location = loc_el.get_text(strip=True) if loc_el else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",  # not exposed anywhere by Bloomberg's careers site
            "application_url": href,
        })

    return jobs


def _fill_cache_for_keyword(keyword: str, timeout: int) -> list[dict]:
    key = keyword.strip().lower()
    if key in _cache:
        return _cache[key]

    collected: list[dict] = []
    seen_ids: set[str] = set()
    offset = 0

    for _ in range(_MAX_PAGES_PER_KEYWORD):
        url = (
            f"{_SEARCH_BASE}/{quote(keyword)}"
            f"?listFilterMode=1&jobRecordsPerPage={_SITE_PAGE_SIZE}&jobOffset={offset}"
        )
        r = _get(url, timeout)
        page_jobs = _parse_search_page(r.text)
        if not page_jobs:
            break

        new_this_page = 0
        for job in page_jobs:
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            new_this_page += 1
            collected.append(job)

        if new_this_page == 0:
            break  # pagination stalled/wrapped -- stop (UBS lesson)

        offset += _SITE_PAGE_SIZE
        time.sleep(0.15)

    _cache[key] = collected
    return collected


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
    jobs = _fill_cache_for_keyword(keyword, timeout)
    return jobs[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the job description from the plain HTML detail page.

    The same URL used as application_url serves the full description -- no
    separate JSON API exists for this ATS. Posting date is always "" -- not
    exposed anywhere on this ATS (same situation as ibm_fetcher.py).
    """
    r = _get(application_url, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    sections = soup.find_all("article", class_="article--details")
    texts = [" ".join(s.get_text(separator=" ").split()) for s in sections]
    # Pick the longest block -- the real "Description & Requirements" content,
    # not the title-only or compact Location/Business-Area/Ref# meta blocks
    # (Honeywell lesson: guard against grabbing a tiny label div by accident).
    description = max(texts, key=len, default="")
    if len(description) < 100:
        description = " ".join(texts).strip()

    return description, ""
