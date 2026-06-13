"""
Icertis job fetcher — Oracle HCM Cloud Candidate Experience REST API.

Tenant: iaaviz.fa.ocs.oraclecloud.com  |  Site: Jobs-at-Icertis
No India facet ID used — fetches globally and filters by "india" in location.
"""
from __future__ import annotations

import re
import time

import requests

_BASE_URL = "https://iaaviz.fa.ocs.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/Jobs-at-Icertis/job"

_SITE_NUMBER = "Jobs-at-Icertis"
_PAGE_SIZE = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "ora-irc-language": "US",
    "Referer": f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/Jobs-at-Icertis/jobs",
}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    finder = (
        f"findReqs;siteNumber={_SITE_NUMBER},"
        f"facetsList=LOCATIONS,"
        f"limit={num},"
        f"offset={start},"
        f'keyword="{keyword}",'
        f"sortBy=RELEVANCY"
    )
    params = {
        "onlyData": "true",
        "expand": "requisitionList.workLocation,requisitionList.secondaryLocations",
        "finder": finder,
    }

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
                raise RateLimitError(f"Icertis search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    data = r.json()
    items = data.get("items", [])
    if not items:
        return []

    req_list = items[0].get("requisitionList", [])

    jobs = []
    for j in req_list:
        job_id = j.get("Id", "")
        if not job_id:
            continue
        location_str = j.get("PrimaryLocation", "") or ""
        if "india" not in location_str.lower():
            continue
        posted_date = (j.get("PostedDate") or "")[:10]
        jobs.append({
            "id": str(job_id),
            "title": j.get("Title", "").strip(),
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
