"""Fetches Indegene job listings via SAP SuccessFactors' "Job2Web Unify"
theme REST API (same platform family as Standard Chartered/Wipro/HCLTech).

Indegene's public marketing site (`www.indegene.com/careers`) links out to
`careers.indegene.com` (SAP SuccessFactors tenant, company code
`indegenepr`). That domain actually hosts *two* different UIs for the same
underlying tenant:

  - `career44.sapsf.com/career?company=indegenepr` -- a legacy DWR-based
    "MySTEPS"-branded classic candidate portal (confirmed working, reports
    the correct total job count) that the marketing site's "Search Jobs"
    button posts to, then 302-redirects to `careers.indegene.com` for the
    results view.
  - `careers.indegene.com/search/` -- the real results-rendering surface,
    Unify-themed (empty `data-row` HTML, CSRF token in a
    `var CSRFToken = "...";` assignment), same shape as Standard Chartered.

Discovered via Playwright network capture (no other approach found the
right request): `POST /services/recruiting/v1/jobs` with a JSON body needs
`categoryId` as the **integer** `0`, not the empty string `""` used by the
appParams template embedded in the page -- sending `""` makes the tenant
silently fall back to re-serving the SPA shell HTML (HTTP 200, useless
content) instead of erroring, which is what made this tenant look totally
broken until the literal browser-issued request was captured and diffed
against a hand-built one field at a time.

No country-level facet key like `jobLocationCountry` exists here; the
country field is `filter1` (labelled "Job search country" in the page's own
facet config) and takes the literal string "India". Small pool (~23 India
jobs) -- fetch full pool once per process and cache, same idea as UBS/
Deutsche Bank/Persistent. `filter1`/search results carry country only, no
city -- a small sample of full JDs showed only "Bangalore" ever named
explicitly (never Chennai/Pune/Kochi/Chandigarh/Tamil Nadu), so a location
string of plain "India" is used and `matcher.py`'s Layer 1 does the real
work; unlike Deltatre this can't be guaranteed forever, so worth rechecking
if Indegene ever opens a Chennai/Pune desk.

Job detail pages are server-rendered plain HTML at
`/job/{urlTitle}/{id}-en_GB` -- description lives in the "Job Description:"
joblayouttoken span (Standard Chartered's exact extraction pattern);
posting date is the "Posting Start Date:" joblayouttoken (DD/MM/YYYY).
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import datetime

import requests

_BASE_URL = "https://careers.indegene.com"
_SEARCH_PAGE = f"{_BASE_URL}/search/"
_SEARCH_URL = f"{_BASE_URL}/services/recruiting/v1/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Module-level cache: filled once (session + CSRF token are per-process
# cheap to hold), reused for every keyword call -- same pattern as
# standardchartered_fetcher.py.
_cache: dict[str, dict] = {}
_cache_filled: bool = False
_session: requests.Session | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from Indegene's SF tenant."""


def _get_session_and_csrf(timeout: int) -> tuple[requests.Session, str]:
    s = requests.Session()
    s.headers.update(_HEADERS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = s.get(_SEARCH_PAGE, timeout=timeout, verify=False)
    r.raise_for_status()
    m = re.search(r'CSRFToken\s*=\s*"([^"]+)"', r.text)
    if not m:
        raise RateLimitError("Indegene: CSRF token not found on search page")
    return s, m.group(1)


def _parse_start_date(raw: str) -> str:
    """'23/07/2026' -> '2026-07-23'."""
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled, _session
    _cache_filled = True  # set before try -- avoid retry storms on failure

    try:
        s, csrf = _get_session_and_csrf(timeout)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "x-csrf-token": csrf,
            "Referer": _SEARCH_PAGE,
            "Origin": _BASE_URL,
        }
        page_number = 0
        while True:
            body = {
                "locale": "en_GB", "pageNumber": page_number, "sortBy": "", "keywords": "",
                "location": "", "facetFilters": {"filter1": ["India"]},
                "brand": "", "skills": [], "categoryId": 0, "alertId": "", "rcmCandidateId": "",
            }
            for attempt in range(3):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = s.post(_SEARCH_URL, headers=headers, json=body, timeout=timeout, verify=False)
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Indegene: 429 rate-limited")
                r.raise_for_status()
                break

            page_results = r.json().get("jobSearchResult", [])
            if not page_results:
                break
            for jr in page_results:
                j = jr.get("response", {})
                jid = j.get("id", "")
                if jid:
                    _cache[jid] = j
            page_number += 1
            time.sleep(0.2)

        _session = s
    except requests.RequestException as exc:
        raise RateLimitError(f"Indegene cache fill failed: {exc}") from exc


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return Indegene India job listings.

    `facetFilters.filter1` (server-side) already restricts to India; the
    tenant's own search is not queried with `keywords` server-side (small
    pool, cached whole once) -- matcher.py's dedup + title/skill filters do
    the real narrowing, same as Standard Chartered.
    """
    if start > 0:
        return []

    if not _cache_filled:
        _fill_cache(timeout)

    jobs: list[dict] = []
    for jid, j in _cache.items():
        title = (j.get("unifiedStandardTitle") or "").strip()
        if not title:
            continue
        url_title = j.get("urlTitle", "")
        app_url = f"{_BASE_URL}/job/{url_title}/{jid}-en_GB" if url_title else ""
        jobs.append({
            "id": jid,
            "title": title,
            "location": "India",
            "posting_date": _parse_start_date(j.get("unifiedStandardStart", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch full job description + posting date from the server-rendered detail page."""
    sess = _session or requests.Session()
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = sess.get(application_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Indegene description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Indegene description fetch failed: {exc}") from exc

    m = re.search(
        r'Job Description:\s*</span>\s*<span[^>]*>([\s\S]*?)</span>\s*</div>\s*</div>\s*</div>\s*</div>',
        r.text,
    )
    description = ""
    if m:
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        description = " ".join(text.split())

    date_match = re.search(
        r'Posting Start Date:\s*</span>\s*<span[^>]*>\s*([\d/]+)', r.text
    )
    posting_date = _parse_start_date(date_match.group(1)) if date_match else ""

    return description, posting_date
