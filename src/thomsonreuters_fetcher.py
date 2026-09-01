"""Fetches Thomson Reuters job listings via the Workday public REST API.

Thomson Reuters' ATS is Workday, hosted at
thomsonreuters.wd5.myworkdayjobs.com, site "External_Career_Site" (found by
following the redirect from jobs.thomsonreuters.com). Standard Workday CXS
REST shape -- plain POST, no browser required.

India is filtered server-side via the `Location_Country` facet -- a
top-level (not nested under locationMainGroup) facet key, same shape as
State Street/Marsh McLennan. India WID `c4f78be1a8f14da0ab49ce1162348a5e` is
the SAME cross-tenant GUID reused across many other tenants in this repo
(Wells Fargo, Citi, Fidelity, Northern Trust, MUFG, Automation Anywhere).
Verified live 2026-08-31: 39 of 391 total global postings are India
(Bengaluru/Hyderabad/Mumbai).

**Max page size is 20** -- `limit` values of 30/50 both return a bare HTTP
400 with no useful message (same "silent cap" family as Northern Trust);
`limit=20` works cleanly. Silently capped here rather than surfaced as an
error.

This search response's jobPosting entries carry NO `locationsText` field at
all (unlike Wells Fargo's shape) -- only `bulletFields`, a 3-element array of
[city list, state list, requisition ID] (e.g. ["Hyderabad; Bangalore",
"Karnataka; Telangana", "JREQ201915"]). Location text handed to matcher.py is
built as "{city list}, India" using bulletFields[0], since the India facet
already guarantees the country and there's no separate country string to
read.

Job ID: bulletFields' LAST element matching `JREQ\\d+` (position varies --
this tenant's array is [cities, states, reqId], not the single-element shape
Automation Anywhere/Wells Fargo use), with a regex-on-externalPath fallback.

Titles/descriptions carry strong, explicit signal on BOTH tracks -- Thomson
Reuters' CoCounsel legal-AI product drives real LangChain/LangGraph/RAGAS/
vector-database/ChromaDB/Pinecone postings ("Automation and AI Developer",
Bengaluru: explicit "LangGraph agentic workflows on AWS AgentCore", "custom
RAG pipelines", "vector retrieval"), while a separate .NET track is equally
real ("Associate Lead Software Engineer -C#, .net, Angular", "Lead Engineer
C++/C#"). Titles are specific, not generic level-banded IT-services titles,
so require_tech_in_description is NOT enabled.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://thomsonreuters.wd5.myworkdayjobs.com"
_TENANT = "thomsonreuters"
_SITE = "External_Career_Site"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"

# Workday rejects limit > 20 for this tenant with a bare HTTP 400.
_PAGE_SIZE = 20

# India Location_Country WID -- same cross-tenant GUID reused across many
# Workday tenants in this repo. Verified live 2026-08-31.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

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


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


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


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    body = {
        "appliedFacets": {"Location_Country": [_INDIA_WID]},
        "limit": min(num, _PAGE_SIZE),
        "offset": start,
        "searchText": keyword,
    }

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    json=body,
                    timeout=timeout,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Thomson Reuters Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Thomson Reuters fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")
        bullets = p.get("bulletFields", []) or []

        job_id = ""
        for field in reversed(bullets):
            m = re.match(r"^(JREQ\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(JREQ\d+)", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        # No locationsText in this tenant's response shape -- bulletFields[0]
        # is the city list; India is already guaranteed by the applied facet.
        city_text = (bullets[0].strip() if bullets else "") or "India"
        loc = city_text if "india" in city_text.lower() else f"{city_text}, India"

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Thomson Reuters description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Thomson Reuters description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
