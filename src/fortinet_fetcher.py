"""Fetches Fortinet job listings via the Oracle HCM Cloud CE REST API.

Fortinet's careers page (fortinet.com/corporate/careers) embeds an Oracle
Fusion/HCM Candidate Experience widget at
edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001
(found live 2026-09-13 via the page's own outbound requests -- not guessed).
Plain HTTP works, same REST pattern as Chubb/BNY/WTW/Amex.

This tenant's location facet only surfaces its top-10-by-volume countries
(US/Canada/Germany/Japan/France/...) -- India's share is too small to ever
appear there, the same "no usable location facet" shape already documented
for Genpact/WTW. Unlike WTW, though, every requisition DOES carry a reliable
`PrimaryLocationCountry` ISO code (e.g. "IN"), which sidesteps the
"Indianapolis" substring trap entirely (WTW/Genpact only had free-text
`PrimaryLocation` to filter on). The whole ~930-job global pool is small
enough to cache in ~5 requests (limit=200/page) and filter by
`PrimaryLocationCountry == "IN"` once, rather than repeat a keyword search
per config keyword (`keyword=` genuinely narrows server-side here, but
doing so still requires a global fetch per term since there is no India
facet to combine it with).

Confirmed live: 34 India postings (of 930), a mix of sales/pre-sales/
professional-services and a handful of genuine engineering roles ("Staff
Software Development Engineer", "Staff Devops Engineer", "Cyber Threat
Researcher") in Bangalore/Gurgaon/Mumbai/Pune/Chennai/Hyderabad/Surat.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://edel.fa.us2.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX_2001/job"

_SITE_NUMBER = "CX_2001"
_FILL_PAGE_SIZE = 200

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "ora-irc-language": "US",
    "Referer": f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX_2001/jobs",
}

_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Oracle HCM CE."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _get_with_retry(params: dict, timeout: int, what: str, url: str = _SEARCH_URL) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Fortinet {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Fortinet {what} failed: {exc}") from exc
    raise RateLimitError(f"Fortinet {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    offset = 0
    for _ in range(20):  # defensive cap: 20 * 200 = 4000 jobs, far above the ~930 real pool
        finder = (
            f"findReqs;siteNumber={_SITE_NUMBER},"
            f"limit={_FILL_PAGE_SIZE},offset={offset},sortBy=POSTING_DATES_DESC"
        )
        params = {"onlyData": "true", "expand": "requisitionList", "finder": finder}
        r = _get_with_retry(params, timeout, f"cache fill offset={offset}")
        item = r.json().get("items", [{}])[0]
        reqs = item.get("requisitionList", [])
        if not reqs:
            break
        total = item.get("TotalJobsCount", 0)

        for j in reqs:
            country = (j.get("PrimaryLocationCountry") or "").strip().upper()
            if country != "IN":
                continue
            job_id = j.get("Id", "")
            if not job_id:
                continue
            collected.append({
                "id": str(job_id),
                "title": (j.get("Title") or "").strip(),
                "location": j.get("PrimaryLocation") or "India",
                "posting_date": (j.get("PostedDate") or "")[:10],
                "application_url": f"{_JOB_BASE}/{job_id}",
            })

        offset += _FILL_PAGE_SIZE
        if offset >= total:
            break

    _india_cache[:] = collected
    print(f"[Fortinet] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Fortinet India jobs.

    No India-country location facet exists on this tenant (too small a
    share to surface in the top-10 facet list), so the full global pool is
    fetched once and filtered client-side by PrimaryLocationCountry=="IN";
    keyword/location arguments are accepted for interface compatibility.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    job_id = application_url.rstrip("/").split("/")[-1]

    finder = f'ById;Id="{job_id}",siteNumber={_SITE_NUMBER}'
    params = {"expand": "all", "onlyData": "true", "finder": finder}

    r = _get_with_retry(params, timeout, f"description fetch {job_id}", url=_DETAIL_URL)
    items = r.json().get("items", [])
    if not items:
        raise RateLimitError(f"Fortinet description: empty response for job {job_id}")

    job = items[0]
    desc_parts = [
        job.get("ExternalDescriptionStr") or "",
        job.get("ExternalResponsibilitiesStr") or "",
        job.get("ExternalQualificationsStr") or "",
    ]
    description = _strip_html(" ".join(p for p in desc_parts if p))

    raw_date = job.get("ExternalPostedStartDate") or ""
    posting_date = raw_date[:10] if raw_date else ""

    return description, posting_date
