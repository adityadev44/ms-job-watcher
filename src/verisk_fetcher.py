"""
Verisk Analytics job fetcher — Oracle HCM Cloud Candidate Experience REST API.

Plain HTTP requests, no Playwright needed. Same API pattern as chubb_fetcher.py
but different tenant (fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com) and site (CX_1).

Verified live (2026-09-08):
- careers.verisk.com's own "Search Jobs" button links straight to
  fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions
- Search API returns real, current Hyderabad, Telangana, India postings
  (e.g. "Sr. Software Engineer - .NET, P&C/Insurance", "Senior Software Architect").
- The India location facet (selectedLocationsFacet) is authoritative here: applying
  it plus a non-empty keyword returns a small, genuinely-India result set with no
  observed non-India leakage (unlike Micron/Verizon/Lowe's — see PLAYBOOK.md).
- An EMPTY keyword ("") combined with the India facet returns TotalJobsCount: 0
  (same class of quirk as Wells Fargo's limit=0 bug) — always send a real keyword.
- Detail API (recruitingCEJobRequisitionDetails) returns full HTML job description
  fields directly; no Playwright/JS-rendering needed for descriptions either.
"""
from __future__ import annotations

import re
import time

import requests

_BASE_URL = "https://fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX_1/job"

_SITE_NUMBER = "CX_1"
_INDIA_LOCATION_FACET_ID = 300000000455977
_PAGE_SIZE = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "ora-irc-language": "US",
    "Referer": f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
}

_INDIA_RE = re.compile(r"\bindia\b", re.IGNORECASE)


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _is_india_location(location: str) -> bool:
    return bool(_INDIA_RE.search(location or ""))


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    # An empty/blank keyword combined with the India facet returns 0 results on
    # this tenant (observed live) -- fall back to a broad term instead of sending "".
    kw = (keyword or "").strip() or "engineer"

    finder = (
        f"findReqs;siteNumber={_SITE_NUMBER},"
        f"facetsList=LOCATIONS,"
        f"limit={num},"
        f"offset={start},"
        f'keyword="{kw}",'
        f"sortBy=RELEVANCY,"
        f"selectedLocationsFacet={_INDIA_LOCATION_FACET_ID}"
    )
    params = {
        "onlyData": "true",
        "expand": "requisitionList.workLocation,requisitionList.secondaryLocations",
        "finder": finder,
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"Verisk search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    data = r.json()
    items = data.get("items", [])
    if not items:
        return []

    search_obj = items[0]
    req_list = search_obj.get("requisitionList") or []

    jobs = []
    seen_ids = set()
    for j in req_list:
        job_id = j.get("Id", "")
        if not job_id or str(job_id) in seen_ids:
            continue
        location_str = j.get("PrimaryLocation", "") or ""
        if not _is_india_location(location_str):
            continue
        seen_ids.add(str(job_id))
        posted_date = (j.get("PostedDate") or "")[:10]
        jobs.append({
            "id": str(job_id),
            "title": (j.get("Title") or "").strip(),
            "location": location_str,
            "posting_date": posted_date,
            "application_url": f"{_JOB_BASE}/{job_id}",
        })
    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    job_id = application_url.rstrip("/").split("/")[-1]

    finder = f'ById;Id="{job_id}",siteNumber={_SITE_NUMBER}'
    params = {
        "expand": "all",
        "onlyData": "true",
        "finder": finder,
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_DETAIL_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on detail for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    data = r.json()
    items = data.get("items", [])
    if not items:
        return "", ""

    job = items[0]
    desc_parts = [
        job.get("ExternalDescriptionStr") or "",
        job.get("ExternalResponsibilitiesStr") or "",
        job.get("ExternalQualificationsStr") or "",
    ]
    combined_html = " ".join(p for p in desc_parts if p)
    description = _strip_html(combined_html)

    raw_date = job.get("ExternalPostedStartDate") or ""
    posting_date = raw_date[:10] if raw_date else ""

    return description, posting_date
