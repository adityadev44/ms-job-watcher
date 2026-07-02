"""Fetches Wipro job listings via SAP SuccessFactors' "Job2Web Unify" theme
REST API -- same underlying platform/API shape as standardchartered_fetcher.py
(see that module's docstring for how this was discovered), different tenant.

careers.wipro.com/search/ posts to /services/recruiting/v1/jobs with
categoryId=0 (Wipro has no per-category landing pages like Standard
Chartered's "Experienced-Professional-jobs" -- the plain search page covers
everything) and locale "en_US". CSRF token + session cookie come from GETting
/search/ once. `facetFilters: {"jobLocationCountry": ["India"]}` is the only
server-side filter that works (`keywords`/`location` are ignored).

Wipro is a huge IT-services shop -- ~2600 India "software engineer"-adjacent
postings, overwhelmingly generic ("SOFTWARE ENGINEER L3", "SOFTWARE ENGINEER
L4" etc. with no tech named). require_tech_in_title is MANDATORY, same as
TCS/Infosys/Cognizant/Capgemini/Accenture.

locationsText uses office codes like "Bengaluru, IND-29, IND, 560035<br/>"
with no literal "India" word and an embedded <br/> tag -- both handled in
fetch_jobs. Job detail pages are server-rendered HTML, same structure as
Standard Chartered's (itemprop="description" span; "Posting Start Date"
joblayouttoken row).
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import datetime

import requests

_BASE_URL = "https://careers.wipro.com"
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

_cache: dict[str, dict] = {}
_cache_filled: bool = False
_session: requests.Session | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from Wipro's SF tenant."""


def _get_session_and_csrf(timeout: int) -> tuple[requests.Session, str]:
    s = requests.Session()
    s.headers.update(_HEADERS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = s.get(_SEARCH_PAGE, timeout=timeout, verify=False)
    r.raise_for_status()
    m = re.search(r'CSRFToken\s*=\s*"([^"]+)"', r.text)
    if not m:
        raise RateLimitError("Wipro: CSRF token not found on search page")
    return s, m.group(1)


def _parse_start_date(raw: str) -> str:
    """Wipro's locale is en_US -- dates render as 'M/D/YY' (e.g. '1/15/26')."""
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
            "Referer": _SEARCH_PAGE,
            "Origin": _BASE_URL,
        }
        page_number = 0
        while True:
            body = {
                "locale": "en_US", "pageNumber": page_number, "sortBy": "", "keywords": "",
                "location": "", "facetFilters": {"jobLocationCountry": ["India"]},
                "brand": "", "skills": [], "categoryId": 0,
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
                    raise RateLimitError("Wipro: 429 rate-limited")
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
        raise RateLimitError(f"Wipro cache fill failed: {exc}") from exc


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return Wipro India job listings (facetFilters-narrowed, cached once)."""
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
        raw_loc = locs[0] if locs else ""
        loc = re.sub(r"<br\s*/?>", "", raw_loc).strip() or "India"
        if "india" not in loc.lower():
            loc = f"{loc}, India"
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
                raise RateLimitError("Wipro description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Wipro description fetch failed: {exc}") from exc

    # NOTE: itemprop="description" is unreliable on this tenant -- it also
    # tags an unrelated "About Wipro" company blurb earlier in the page.
    # Anchor on the "Job Description:" joblayouttoken label instead.
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
