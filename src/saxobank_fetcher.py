"""Fetches Saxo Bank job listings via the Workday public REST API.

Saxo Bank's branded careers page (`www.home.saxo/about-us/careers`) embeds a
widget CSS-themed "workday" that is NOT the real Workday CXS API — its own
`data-baseurl` points at a custom aggregator
(`www.saxotrader.com/oapi/jobsandholidays/v1/jobs/getjobcategories`), a
category-lookup endpoint only, not a job search. Live inspection of the same
page's plain "See job openings" link, however, resolves to
`saxobank.wd3.myworkdayjobs.com/CareeratSaxoBank` — a genuine Workday tenant
(site `CareeratSaxoBank`). Confirmed directly with a plain unauthenticated
`requests` POST (2026-09-05):

    POST https://saxobank.wd3.myworkdayjobs.com/wday/cxs/saxobank/CareeratSaxoBank/jobs
    body {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    -> HTTP 200, {"total": 40, "jobPostings": [...], "facets": [...]}

Same CXS REST pattern as SimCorp/Genpact/Wells Fargo — no Playwright needed.

Key findings, all verified live rather than assumed:

- **No usable location facet.** The only facets this tenant exposes are
  `Skills`, `timeType`, and `jobFamilyGroup` (no country/location facet at
  all) — same shape as SimCorp/Genpact. Every posting is fetched globally and
  India is detected client-side from `locationsText`.
- **`searchText` genuinely filters server-side** (A/B verified): empty query
  -> 40/40, `"dotnet"` -> 2, `"engineer"` -> 13, a nonsense token
  (`"asdkjaslkdjqwe"`) -> 0. Despite this, the fetcher deliberately does
  *not* thread the caller's `keyword` through to the API and instead caches
  the whole board once per process (see below) — same design call as
  SimCorp, whose board is ~7x bigger. Saxo Bank's entire board is only 40
  postings, so re-querying per keyword (10 keywords x possible multi-page
  pagination) buys nothing a single one-time full fetch doesn't already give
  for free. **Registered in `_IGNORES_KEYWORDS`** for this reason, even
  though the underlying API param is real and working.
- **Pagination silently wraps around past the true total**, same family of
  bug as MUFG/UBS/Nvidia/Pfizer/Walmart/SimCorp: `offset=40` (== total)
  replays `offset=0`'s exact 20 postings verbatim rather than returning an
  empty page. `offset=20` (the real second page) returns 20 different,
  legitimate postings — the wraparound only triggers once `offset >= total`.
  Guarded the same way as SimCorp: memoize page 1's first job id and stop as
  soon as it's seen again.
- **`limit` hard-caps at 20 server-side with a clean `HTTP_400`** for any
  higher value (verified: `limit: 50` -> `{"errorCode": "HTTP_400", ...}`) —
  same clean-400 shape as Northern Trust, not Shell's silent-empty-200
  variant. Fetcher never requests more than 20 per page.
- **Job IDs come back clean in the search response itself**
  (`bulletFields: ["R-19118"]`), unlike SimCorp where the same field had to
  be regex-matched out of a longer string — used directly, with the same
  regex-on-`externalPath` fallback kept as defense-in-depth in case a future
  posting omits `bulletFields`.
- **Saxo Bank's only India office is Gurugram** (Haryana) — confirmed two
  ways: (1) every one of the 40 live postings' `locationsText` is a plain
  city name or "Headquarters" (Copenhagen, Denmark — confirmed via a job
  detail fetch showing `country.descriptor: "Denmark"` for a Headquarters
  posting vs `"India"` for a Gurugram one) with zero ambiguous "N Locations"
  labels anywhere in the current pool; (2) the branded careers page's own
  office list names exactly one Indian city verbatim ("...Copenhagen,
  Dubai, Zürich, Antwerp, Prague, Milan, London, Amsterdam, Singapore,
  **Gurgaon**, Tokyo, and Paris..."). `locationsText` never contains an
  "India" substring by itself (just "Gurugram"), so the fetcher appends
  ", India" whenever the city matches a small whitelist (`gurugram`,
  `gurgaon` — the pre- and post-2016-rename spellings, both Saxo's own
  material uses the older "Gurgaon" while the live Workday tenant uses
  "Gurugram"). A defensive ambiguous-location resolver (SimCorp's "N
  Locations" pattern) is kept for any future multi-site Saxo posting, even
  though none exist in the current pool.
- Of the 7 current Gurugram postings, only 2 (`Senior Dot Net Developer`,
  `Senior Dotnet Developer`) pass the shared `title_family` check today.
  **New title_family precision-gap instances found, flagged not fixed**
  (same backlog as PLAYBOOK's "Near-Miss Audit Tool" findings): "Senior Site
  Reliability Engineer (SRE) – Observability & Platform Systems" and
  "Outsystems Platforms - DevOps Engineer" are real hands-on engineering
  roles that never match any `title_family` phrase ("devops engineer" and
  "site reliability engineer" are not in the list — "devops engineer" was
  already independently identified as a high-impact gap in the 2026-09-04
  near-miss audit's `title_family_candidate_impact.csv`). Not fixed here.

Job descriptions are NOT inline in the search response (only
title/externalPath/locationsText/postedOn/bulletFields) — fetched from the
same Workday CXS detail endpoint
(`GET /wday/cxs/saxobank/CareeratSaxoBank{externalPath}`), which also
returns a clean `startDate` (already `YYYY-MM-DD`) that overwrites the
search listing's relative-date parse ("Posted 5 Days Ago" etc.) with an
exact value.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://saxobank.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/saxobank/CareeratSaxoBank/jobs"
_JOB_BASE = f"{_BASE_URL}/CareeratSaxoBank"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/saxobank/CareeratSaxoBank"
_PAGE_SIZE = 20
_MAX_PAGES = 10  # safety ceiling (~200 jobs) — real pool is ~40

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

# Saxo Bank's only confirmed India office, as it appears verbatim in
# Workday's locationsText ("Gurugram") plus the pre-2016-rename spelling
# Saxo's own marketing page still uses ("Gurgaon") — see module docstring.
_INDIA_CITIES = {"gurugram", "gurgaon"}

# Module-level cache: the full board is resolved once per process and reused
# for every keyword/location call — this tenant has no location facet at all
# and the whole board (~40 jobs) is far cheaper to fetch once than to
# re-paginate per keyword for no benefit (see module docstring).
_india_cache: list[dict] = []
_cache_filled: bool = False
_desc_cache: dict[str, tuple[str, str]] = {}


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
            r = requests.post(url, headers=_HEADERS, json=body, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Saxo Bank {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Saxo Bank {label} failed: {exc}") from exc
    raise RateLimitError(f"Saxo Bank {label}: no response — {last_exc}")


def _get_with_retries(url: str, timeout: int, label: str):
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Saxo Bank {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Saxo Bank {label} failed: {exc}") from exc
    raise RateLimitError(f"Saxo Bank {label}: no response — {last_exc}")


def _resolve_ambiguous_location(external_path: str, timeout: int) -> str | None:
    """Resolve an ambiguous "N Locations" posting's true country via its
    detail page. None of Saxo Bank's current 40 postings use this shape
    (unlike SimCorp), but the same defensive resolver is kept in case a
    future multi-site req collapses this way — see module docstring.
    """
    try:
        r = _get_with_retries(f"{_DETAIL_BASE}{external_path}", timeout, "location resolve")
    except RateLimitError:
        return None
    info = r.json().get("jobPostingInfo", {})
    country = (info.get("country") or {}).get("descriptor", "")
    if country.strip().lower() != "india":
        return None
    return "India"


_AMBIGUOUS_LOCATION_RE = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)


def _fill_cache(timeout: int = 20) -> None:
    """Paginate the full Saxo Bank board once and cache genuine India postings.

    ``_cache_filled`` is set before the loop so a mid-run failure doesn't
    cause a retry storm on every subsequent ``fetch_jobs()`` call in this
    same process (Honeywell/Persistent lesson) — a scan cycle that hits an
    error here simply yields no Saxo Bank jobs this cycle and self-heals on
    the next 30-minute run.
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    # Like MUFG/UBS/Nvidia/Pfizer/Walmart/SimCorp, this tenant's pagination
    # wraps around past the true total instead of returning an empty page
    # (verified live: offset 40 == total silently replays offset 0's exact 20
    # postings, forever). Memoize page 1's first job id and stop as soon as
    # it reappears.
    all_postings: list[dict] = []
    offset = 0
    first_page_anchor: str | None = None
    for page_num in range(_MAX_PAGES):
        if page_num > 0:
            time.sleep(0.15)
        body = {"appliedFacets": {}, "limit": _PAGE_SIZE, "offset": offset, "searchText": ""}
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
        elif _AMBIGUOUS_LOCATION_RE.match(loc_text):
            time.sleep(0.1)
            location = _resolve_ambiguous_location(external_path, timeout)
        else:
            location = None  # a known non-India single location — skip, no request needed

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
    print(f"[Saxo Bank] Cache filled: {len(collected)} India jobs (of {len(all_postings)} total)")


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
    """Return a page of Saxo Bank's India job postings.

    Keyword is ignored: the Workday tenant's own ``searchText`` genuinely
    filters server-side (verified — see module docstring), but there is no
    location facet at all and the whole board is only ~40 postings, so
    caching the full pool once and slicing it here is simpler and strictly
    cheaper than re-querying per keyword for the same result.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date via the Workday CXS detail API."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    if application_url.startswith(_JOB_BASE):
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/CareeratSaxoBank/", 1)[-1]

    r = _get_with_retries(f"{_DETAIL_BASE}{ext_path}", timeout, "description")
    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
