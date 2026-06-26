"""Fetches UBS job listings via the IBM BrassRing TalentGateway REST API.

UBS's ATS is IBM BrassRing (Kenexa), hosted at jobs.ubs.com.  The portal is
a .NET Angular SPA that calls a JSON REST endpoint:

    POST https://jobs.ubs.com/TgNewUI/Search/Ajax/PowerSearchJobs

Each request requires:
  - A fresh CSRF token ("RFT" header) obtained from the search page HTML
  - Session cookies set by the same GET request

The response embeds job descriptions inline (the "jobdescription" Questions
field), so no separate description-fetch HTTP call is needed.  Results are
cached in-module after the first call so every keyword iteration is free.

Key fields per job (inside the Questions array):
  reqid        → unique job ID
  jobtitle     → job title
  formtext23   → location (country-level; typically "India")
  lastupdated  → date in "DD-Mon-YYYY" format (e.g. "26-Jun-2026")
  jobdescription → HTML job description (inline — no detail-fetch needed)

The Link field on each job object contains the full application URL.

Pagination quirk: the API returns 50 results per page.  However, incrementing
PageNumber for a given session appears to return overlapping result sets.  The
safest strategy is to search with each configured keyword and aggregate unique
jobs by reqid — all India jobs visible in the portal appear in the first page
of at least one keyword search.
"""

from __future__ import annotations

import html as html_mod
import re
import time
import warnings
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_PORTAL_URL = (
    "https://jobs.ubs.com/TGnewUI/Search/home/Home"
    "?partnerid=25008&siteid=5012"
)
_SEARCH_URL = "https://jobs.ubs.com/TgNewUI/Search/Ajax/PowerSearchJobs"
_PAGE_SIZE = 50  # BrassRing default; server-enforced

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Module-level cache — filled once per process run.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}  # url → (description, posting_date)
_cache_filled = False


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent failure from the BrassRing API."""


def _strip_html(raw: str) -> str:
    """Strip HTML tags and decode entities, returning normalised plain text."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(date_str: str) -> str:
    """Convert BrassRing date format 'DD-Mon-YYYY' to 'YYYY-MM-DD'."""
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _get_session_and_token() -> tuple[requests.Session, str]:
    """Create a requests session, load the BrassRing portal page, and extract
    the CSRF token (RFT) required for POST requests."""
    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = session.get(_PORTAL_URL, timeout=20, verify=False)
    r.raise_for_status()

    # The CSRF token is in a hidden input named __RequestVerificationToken.
    match = re.search(
        r'name="__RequestVerificationToken"[^>]+value="([^"]+)"', r.text
    )
    if not match:
        match = re.search(
            r'value="([^"]+)"[^>]+name="__RequestVerificationToken"', r.text
        )
    token = match.group(1) if match else ""
    return session, token


def _search_page(
    session: requests.Session,
    csrf_token: str,
    keyword: str,
    page: int = 1,
    timeout: int = 20,
) -> list[dict]:
    """POST one search page and return the raw job list from the response."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": _PORTAL_URL,
        "Origin": "https://jobs.ubs.com",
        "RFT": csrf_token,
    }
    body = {
        "PartnerId": "25008",
        "SiteId": "5012",
        "Keyword": [keyword],
        "ListKeyword": [keyword],
        "Location": [""],
        "KeywordCustomSolrFields": None,
        "LocationCustomSolrFields": None,
        "Latitude": 0,
        "Longitude": 0,
        "Radius": 0,
        "FacetFilterFields": {"Facet": []},
        "SortType": "",
        "PageNumber": page,
        "CallType": "SearchButtontype",
        "SocialReferalType": "",
        "PowerSearchOptions": {"PowerSearchOption": []},
        "EncryptedSessionValue": "",
        "localizedStrings": {},
        "JobSiteIds": "",
        "RunSavedSearch": False,
        "TurnOffHttps": False,
        "LinkID": 0,
        "JobCountOnly": False,
        "SearchResumeName": "",
        "MatchedReqIds": [],
        "ClearSession": False,
        "UserGivenKeyWords": keyword,
        "BringAllJobs": False,
        "RequestForMap": False,
    }

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = session.post(
            _SEARCH_URL,
            headers=headers,
            json=body,
            timeout=timeout,
            verify=False,
        )

    if r.status_code == 429:
        raise RateLimitError("UBS BrassRing: 429 rate-limited")
    r.raise_for_status()
    return r.json().get("Jobs", {}).get("Job", [])


def _fill_cache(
    keywords: list[str] | None = None,
    timeout: int = 20,
) -> None:
    """Populate _cache with all unique India UBS jobs found across keywords.

    Descriptions are embedded inline in the BrassRing search results, so this
    also fills _desc_cache keyed by application_url.
    """
    if keywords is None:
        # Default keyword set — covers all India tech roles visible on portal
        keywords = ["", "software engineer", "developer", ".NET", "technology"]

    for attempt in range(3):
        try:
            session, csrf_token = _get_session_and_token()
            break
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(
                    f"UBS: could not load portal page: {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    seen_ids: set[str] = set()

    for kw in keywords:
        for page in range(1, 5):  # guard: max 4 pages per keyword
            try:
                raw_jobs = _search_page(session, csrf_token, kw, page=page, timeout=timeout)
            except RateLimitError:
                raise
            except Exception as exc:
                raise RateLimitError(f"UBS search failed (kw={kw!r}): {exc}") from exc

            if not raw_jobs:
                break

            new_this_page = 0
            for job in raw_jobs:
                qs = {q["QuestionName"]: q["Value"] for q in job.get("Questions", [])}
                job_id = str(qs.get("reqid", "")).strip()
                if not job_id or job_id in seen_ids:
                    continue

                loc = str(qs.get("formtext23", "")).strip()
                if "india" not in loc.lower():
                    continue  # client-side India filter

                seen_ids.add(job_id)
                new_this_page += 1

                title = str(qs.get("jobtitle", "")).strip()
                raw_date = str(qs.get("lastupdated", "")).strip()
                posting_date = _parse_date(raw_date)
                app_url = job.get("Link", "")
                desc_html = str(qs.get("jobdescription", "")).strip()
                description = _strip_html(desc_html)

                _cache.append({
                    "id": job_id,
                    "title": title,
                    "location": loc,
                    "posting_date": posting_date,
                    "application_url": app_url,
                })
                _desc_cache[app_url] = (description, posting_date)

            # BrassRing pagination wraps around — stop paging once no new jobs appear
            if new_this_page == 0:
                break

            time.sleep(0.3)
        time.sleep(0.5)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of the cached India UBS job listings.

    The cache is filled on the first call using a fixed set of keywords that
    covers all India-located roles visible on the BrassRing portal.  The
    caller's keyword/location arguments are accepted for interface compatibility
    but are not used for API filtering — all filtering is client-side.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True
        _fill_cache(timeout=timeout)
    return _cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a job.

    UBS BrassRing embeds the full job description in the search results, so
    this simply reads from the in-memory description cache populated during
    fetch_jobs().  No additional HTTP call is needed.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Fallback: scrape the job detail page (should not normally be reached).
    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    application_url,
                    headers={"User-Agent": _UA},
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                raise RateLimitError("UBS description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            return "", ""

    soup = BeautifulSoup(r.text, "html.parser")
    desc_el = soup.find(class_=re.compile(r"jobdesc|job-desc|description", re.I))
    description = " ".join(desc_el.get_text(separator=" ").split()) if desc_el else ""
    return description, ""
