"""JLL (Jones Lang LaSalle) India job fetcher -- Workday CXS REST API.

JLL is a Fortune 500 commercial real estate and technology company.
Their India operations span Bangalore (large tech hub, 5,000+ employees)
and a new Hyderabad GCC opened Aug 2026 (~1,600 planned hires). India
roles include C# backend services, Python (Django/DRF/FastAPI), and
AI/LLM/RAG engineering (Azure OpenAI, LangChain) -- direct match for
both the .NET/C# and AI/ML/Python tracks.

Careers site: https://jll.wd1.myworkdayjobs.com/jllcareers
Workday tenant: jll, site: jllcareers
Confirmed 2026-10-03 via job URL inspection:
  jll.wd1.myworkdayjobs.com/jllcareers/job/Bengaluru-KA/..._REQ493420

The India country facet uses the standard cross-tenant WID
`c4f78be1a8f14da0ab49ce1162348a5e` (same as Adobe/Citi/Barclays/ABB/
SS&C Technologies already in this repo) -- confirmed from a live job URL:
`?locationCountry=c4f78be1a8f14da0ab49ce1162348a5e`. India filtering is
therefore server-side via `appliedFacets`, not client-side substring
matching.

API shape is the standard Workday CXS pattern used throughout this repo:

    POST /wday/cxs/jll/jllcareers/jobs
    Body: {"searchText": "<keyword>",
           "appliedFacets": {"Location_Country": ["c4f78be1a8f14da0ab49ce1162348a5e"]},
           "limit": 20, "offset": <start>}

- `searchText` narrows server-side (standard Workday behavior).
- Server hard-caps `limit` at 20; requesting more returns HTTP 400.
- Pagination wraparound guard via `_FIRST_PAGE_IDS`.
- Job IDs in `REQ######` format (from `_REQ######` externalPath suffix).
- `postedOn` is Workday's relative string ("Posted 3 Days Ago", etc.) --
  converted to YYYY-MM-DD the same way as other Workday fetchers.
- Job detail via GET /wday/cxs/jll/jllcareers/job/<externalPath>:
  `jobPostingInfo.jobDescription` (HTML) and `.startDate`.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""


_BASE_URL = "https://jll.wd1.myworkdayjobs.com"
_TENANT = "jll"
_SITE = "jllcareers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"

_MAX_LIMIT = 20

# Standard cross-tenant India country facet WID -- confirmed on this tenant.
_INDIA_COUNTRY_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": _JOB_BASE,
}

_desc_cache: dict[str, tuple[str, str]] = {}
_FIRST_PAGE_IDS: set[str] | None = None


def _parse_posted_on(posted_on: str) -> str:
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


def _normalize_location(loc_text: str) -> str:
    loc = (loc_text or "").strip()
    if not loc:
        return "India"
    if "india" in loc.lower():
        return loc
    # Workday often returns "Bengaluru-KA" or "Mumbai-MH" style codes
    loc = re.sub(r"-[A-Z]{2}$", "", loc)
    return f"{loc}, India" if loc else "India"


def _job_id_from_external_path(external_path: str) -> str:
    m = re.search(r"_(REQ\d+)$", external_path, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"_([A-Za-z]+-?\d+)$", external_path)
    return m.group(1).upper() if m else ""


def _post_with_retries(url: str, body: dict, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(url, headers=_HEADERS, json=body, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("JLL search: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"JLL search failed: {exc}") from exc
    raise RateLimitError(f"JLL search: no response -- {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    global _FIRST_PAGE_IDS

    body = {
        "appliedFacets": {"Location_Country": [_INDIA_COUNTRY_WID]},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    r = _post_with_retries(_SEARCH_URL, body, timeout)

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = _normalize_location(p.get("locationsText") or "")

        external_path = p.get("externalPath", "")
        bullet_fields = p.get("bulletFields") or []
        job_id = (bullet_fields[0] if bullet_fields else "") or _job_id_from_external_path(external_path)
        if not job_id:
            continue

        title = (p.get("title") or "").strip()
        if not title:
            continue

        posting_date = _parse_posted_on(p.get("postedOn", ""))
        application_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    if f"{_JOB_BASE}/" in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("JLL description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"JLL description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"JLL description: no response -- {last_exc}")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )
    posting_date = (info.get("startDate") or "")[:10]

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
