"""Fetches Standard Chartered job listings via SAP SuccessFactors' newer
"Job2Web Unify" theme REST API (not the classic HTML data-row template used
by Nomura/Capgemini).

careers.standardchartered.com/careers loads the search-results grid via a
client-side JS call, not server-rendered HTML, so the classic J2W scraping
approach (data-row rows) finds nothing. Discovered via Playwright network
capture instead:

  1. GET  /go/Experienced-Professional-jobs/9783657/  -- HTML page. Contains
     a `var CSRFToken = "...";` assignment (one-time, per session/cookie).
  2. POST /services/recruiting/v1/jobs  -- JSON body:
       {"locale": "en_GB", "pageNumber": 0, "sortBy": "", "keywords": "",
        "location": "", "facetFilters": {"jobLocationCountry": ["India"]},
        "brand": "", "skills": [], "categoryId": 9783657, "alertId": "",
        "rcmCandidateId": ""}
     Header `x-csrf-token` must match the token from step 1; session cookie
     (JSESSIONID, set automatically by requests.Session) must also match.
     `keywords`/`location` are NOT honoured server-side -- facetFilters is
     the only filter that works, and pageNumber must be walked manually
     (10 results/page) to get the full ~360 India jobs. All are fetched
     once and cached in-module; title/skill filters do the real narrowing.
  3. Job detail pages ARE server-rendered plain HTML at
     /job/{urlTitle}/{id}-{locale} -- description lives in the span with
     itemprop="description" (id="reqdescription" is a red herring: that's
     the cookie-consent widget, present on every page). Posting date comes
     from the "Posting Start Date" joblayouttoken row (DD/MM/YYYY).
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import datetime

import requests

_BASE_URL = "https://jobs.standardchartered.com"
_CATEGORY_ID = 9783657
_CATEGORY_PAGE = f"{_BASE_URL}/go/Experienced-Professional-jobs/{_CATEGORY_ID}/"
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
# cheap to hold), reused for every keyword call.
_cache: dict[str, dict] = {}
_cache_filled: bool = False
_session: requests.Session | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from Standard Chartered's SF tenant."""


def _get_session_and_csrf(timeout: int) -> tuple[requests.Session, str]:
    s = requests.Session()
    s.headers.update(_HEADERS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = s.get(_CATEGORY_PAGE, timeout=timeout, verify=False)
    r.raise_for_status()
    m = re.search(r'CSRFToken\s*=\s*"([^"]+)"', r.text)
    if not m:
        raise RateLimitError("Standard Chartered: CSRF token not found on category page")
    return s, m.group(1)


def _parse_start_date(raw: str) -> str:
    """'30/06/2026' -> '2026-06-30'."""
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
            "Accept": "*/*",
            "x-csrf-token": csrf,
            "Referer": _CATEGORY_PAGE,
            "Origin": _BASE_URL,
        }
        page_number = 0
        while True:
            body = {
                "locale": "en_GB", "pageNumber": page_number, "sortBy": "", "keywords": "",
                "location": "", "facetFilters": {"jobLocationCountry": ["India"]},
                "brand": "", "skills": [], "categoryId": _CATEGORY_ID,
                "alertId": "", "rcmCandidateId": "",
            }
            for attempt in range(3):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = s.post(_SEARCH_URL, headers=headers, json=body, timeout=timeout, verify=False)
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Standard Chartered: 429 rate-limited")
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
        raise RateLimitError(f"Standard Chartered cache fill failed: {exc}") from exc


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return Standard Chartered India job listings.

    facetFilters (server-side) already restricts to India; keywords are not
    honoured server-side so every keyword call returns the same full list --
    matcher.py's dedup + title/skill filters do the real narrowing. The
    cache is filled once on the first call; subsequent start>0 calls signal
    "no more pages".
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
        locs = j.get("jobLocationShort") or []
        loc = (locs[0].strip() if locs else "") or "India"
        if "india" not in loc.lower():
            loc = f"{loc}, India"
        url_title = j.get("urlTitle", "")
        app_url = f"{_BASE_URL}/job/{url_title}/{jid}-en_GB" if url_title else ""
        jobs.append({
            "id": jid,
            "title": title,
            "location": loc,
            "posting_date": "",  # not in search payload; filled from detail fetch
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
                raise RateLimitError("Standard Chartered description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Standard Chartered description fetch failed: {exc}") from exc

    # NOTE: id="reqdescription" is a red herring -- it's the cookie-consent
    # widget's ID, present on every page. Anchor on the "Job Description:"
    # joblayouttoken label instead of itemprop="description" -- some tenants
    # (Wipro) reuse that attribute for an unrelated "About the company" field
    # earlier in the page, so the label is the only reliable anchor.
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
