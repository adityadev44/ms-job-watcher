"""Fetches HCLTech job listings via SAP SuccessFactors' "Job2Web Unify"
theme REST API -- same underlying platform/API shape as
standardchartered_fetcher.py and wipro_fetcher.py (see standardchartered's
docstring for how this pattern was discovered), different tenant with
different field names.

careers.hcltech.com/go/India/9553955/ is HCLTech's India-only category
landing page (categoryId 9553955) -- posting straight to
/services/recruiting/v1/jobs with that categoryId already restricts results
to India, no facetFilters needed. CSRF token + session cookie come from
GETting the category page once.

Field-name quirk: unlike Standard Chartered/Wipro (which expose
jobLocationShort / jobLocationCountry), this tenant's search response uses
custprimecity (city string) and custCountryRegion (list, e.g. ["India"]).

HCLTech has ~8000 India postings under this category, overwhelmingly generic
IT-services titles. require_tech_in_title is MANDATORY, same as
TCS/Infosys/Cognizant/Capgemini/Wipro. Job detail pages are server-rendered
HTML, same structure as Standard Chartered's (itemprop="description" span;
"Posting Start Date" joblayouttoken row).
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import datetime

import requests

_BASE_URL = "https://careers.hcltech.com"
_CATEGORY_ID = 9553955
_CATEGORY_PAGE = f"{_BASE_URL}/go/India/{_CATEGORY_ID}/"
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

_cache: dict[str, dict] = {}
_cache_filled: bool = False
_session: requests.Session | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from HCLTech's SF tenant."""


def _get_session_and_csrf(timeout: int) -> tuple[requests.Session, str]:
    s = requests.Session()
    s.headers.update(_HEADERS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = s.get(_CATEGORY_PAGE, timeout=timeout, verify=False)
    r.raise_for_status()
    m = re.search(r'CSRFToken\s*=\s*"([^"]+)"', r.text)
    if not m:
        raise RateLimitError("HCLTech: CSRF token not found on category page")
    return s, m.group(1)


def _parse_start_date(raw: str) -> str:
    """HCLTech's locale is en_US -- dates render as 'M/D/YY' (e.g. '1/15/26')."""
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled, _session
    _cache_filled = True

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
                "locale": "en_US", "pageNumber": page_number, "sortBy": "", "keywords": "",
                "location": "", "facetFilters": {},
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
                    raise RateLimitError("HCLTech: 429 rate-limited")
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
            time.sleep(0.15)

        _session = s
    except requests.RequestException as exc:
        raise RateLimitError(f"HCLTech cache fill failed: {exc}") from exc


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return HCLTech India job listings (India-only category, cached once)."""
    if start > 0:
        return []

    if not _cache_filled:
        _fill_cache(timeout)

    jobs: list[dict] = []
    for jid, j in _cache.items():
        title = (j.get("unifiedStandardTitle") or "").strip()
        if not title:
            continue
        city = (j.get("custprimecity") or "").strip()
        countries = j.get("custCountryRegion") or []
        country = countries[0] if countries else "India"
        loc = f"{city}, {country}" if city else country
        url_title = j.get("urlTitle", "")
        app_url = f"{_BASE_URL}/job/{url_title}/{jid}-en_US" if url_title else ""
        jobs.append({
            "id": jid,
            "title": title,
            "location": loc,
            "posting_date": "",
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
                raise RateLimitError("HCLTech description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"HCLTech description fetch failed: {exc}") from exc

    # NOTE: itemprop="description" is unreliable on this ATS -- it can also
    # tag an unrelated "About the company" field earlier in the page (seen
    # on Wipro, same underlying platform). Anchor on the "Job Description:"
    # joblayouttoken label instead.
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
