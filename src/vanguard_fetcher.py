"""Vanguard India job fetcher -- Workday CXS REST API.

Vanguard's careers portal (vanguardjobs.com) is a WordPress aggregator;
the actual job data is served by Workday. The India-specific Workday site
is:

    https://vanguard.wd5.myworkdayjobs.com/IndiaCareers

Confirmed live 2026-10-03: tenant `vanguard`, site `IndiaCareers`.
The site name itself scopes results to India, so no location-country
facet override is applied (same convention as Workday tenants with
dedicated India sites in this repo).

API shape is the standard Workday CXS pattern used throughout this
repo (browserstack_fetcher.py, workday_fetcher.py, etc.):

    POST /wday/cxs/vanguard/IndiaCareers/jobs
    Body: {"searchText": "<keyword>", "appliedFacets": {},
           "limit": 20, "offset": <start>}

- `searchText` narrows server-side (standard Workday behavior).
- Server hard-caps `limit` at 20 (requesting more returns HTTP 400).
- Pagination wraparound guard via `_FIRST_PAGE_IDS`.
- `locationsText` is in Workday's typical format; normalised to
  "<City>, India" (the `IND.` prefix is stripped, bare "India,
  <City>" is reordered).
- `postedOn` is Workday's relative string ("Posted 3 Days Ago",
  "Posted Yesterday", etc.) -- converted to YYYY-MM-DD.
- Job detail is fetched via GET /wday/cxs/vanguard/IndiaCareers/job/<externalPath>,
  returning `jobPostingInfo.jobDescription` (HTML) and `.startDate`.
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


_BASE_URL = "https://vanguard.wd5.myworkdayjobs.com"
_TENANT = "vanguard"
_SITE = "IndiaCareers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"

_MAX_LIMIT = 20

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
    if loc.upper().startswith("IND."):
        city = loc[4:].strip()
        return f"{city}, India" if city else "India"
    return "India"


def _job_id_from_external_path(external_path: str) -> str:
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
                raise RateLimitError("Vanguard search: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Vanguard search failed: {exc}") from exc
    raise RateLimitError(f"Vanguard search: no response -- {last_exc}")


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
        "appliedFacets": {},
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
                raise RateLimitError("Vanguard description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Vanguard description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Vanguard description: no response -- {last_exc}")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )
    posting_date = (info.get("startDate") or "")[:10]

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
