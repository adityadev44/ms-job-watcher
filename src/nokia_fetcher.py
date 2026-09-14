"""
Nokia job fetcher -- Oracle HCM Cloud Candidate Experience REST API.

jobs.nokia.com/en/sites/CX_1/... redirects (plain HTTP 302, no browser/JS
needed) to the real backend tenant: fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com,
site CX_1 -- same REST pattern already used by Chubb/Amex/Icertis/Hexaware/
BNY Mellon/Goldman Sachs/JPMorgan in this repo. Confirmed via direct probing
2026-09-14, not assumed from a URL-shape guess.

Nokia has a very large India R&D presence (Bengaluru/Chennai/Gurugram/Noida,
5G/telecom software) and this is confirmed live: the India location facet
(discovered from the response's own `locationsFacet` block, id
300000000471745) reports 159 of the tenant's ~598 global jobs, with a real,
meaningful software-engineering hit rate on page 1 alone -- "Software
Engineer", "Software Development Engineer", "Senior Software Development
Engineer", "Staff SW Development Engineer", "Senior AI Engineer", "Software
Architect - Platform" -- alongside the expected majority of telecom-ops
titles (RAN/OSS/FM specialists, hardware/optical engineers) that won't match
`title_family`, as expected for a telecom-equipment vendor.

Unlike Icertis/Hexaware (no usable India facet, global fetch + client-side
filter), Nokia's `LOCATIONS` facet is genuinely usable and narrows
server-side via `selectedLocationsFacet=<id>` -- used directly instead of a
global fetch, since the global pool would otherwise cost ~24x more requests
per keyword pass for no benefit.

`keyword` is passed through unconditionally like every other Oracle HCM CE
fetcher in this repo; not exhaustively A/B tested for server-side narrowing
here, but harmless either way (dedup handles a redundant full-pool re-fetch).

Detail endpoint mirrors Icertis exactly: `recruitingCEJobRequisitionDetails`
with `ById;Id="<id>",siteNumber=CX_1`; description text lives in
`ExternalDescriptionStr` + `ExternalResponsibilitiesStr` +
`ExternalQualificationsStr` (HTML, stripped here); `PrimaryLocation` is a
clean "India" string; `ExternalPostedStartDate` is a reliable ISO timestamp,
confirmed present on a real sampled job (2026-08-31T10:16:19+00:00), used as
posting_date on the detail fetch (search response's own `PostedDate` field
is already set and is preferred by fetch_jobs()).
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BASE_URL = "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = "https://jobs.nokia.com/en/sites/CX_1/job"

_SITE_NUMBER = "CX_1"
_INDIA_LOCATION_FACET_ID = "300000000471745"
_PAGE_SIZE = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "ora-irc-language": "US",
    "Referer": f"{_JOB_BASE.rsplit('/job', 1)[0]}/requisitions",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Nokia's ATS."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _get(url: str, params: dict, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"429 rate-limited fetching {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Nokia fetch failed: {exc}") from exc
    raise RateLimitError(f"Nokia fetch: no response -- {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    sort = "POSTING_DATES_DESC" if sort_by == "date" else "RELEVANCY"
    finder = (
        f"findReqs;siteNumber={_SITE_NUMBER},"
        f"facetsList=LOCATIONS,"
        f"limit={num},"
        f"offset={start},"
        f'keyword="{keyword}",'
        f"selectedLocationsFacet={_INDIA_LOCATION_FACET_ID},"
        f"sortBy={sort}"
    )
    params = {
        "onlyData": "true",
        "expand": "requisitionList.secondaryLocations",
        "finder": finder,
    }

    r = _get(_SEARCH_URL, params, timeout)
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
        title = (j.get("Title") or "").strip()
        if not title:
            continue
        location_str = j.get("PrimaryLocation") or "India"
        posted_date = (j.get("PostedDate") or "")[:10]
        jobs.append({
            "id": str(job_id),
            "title": title,
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

    try:
        r = _get(_DETAIL_URL, params, timeout)
    except RateLimitError:
        return "", ""

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
