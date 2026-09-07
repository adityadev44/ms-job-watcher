"""Fetches Kotak Mahindra Bank job listings — Oracle HCM Cloud Candidate
Experience REST API.

Kotak's marketing "Digital/IT Jobs" page (kotak.com/en/about-us/careers/
tech-jobs.html) itself links its bank-wide "All Jobs" search to an Oracle
Fusion Recruiting Cloud tenant:

    https://hcbt.fa.em2.oraclecloud.com:443/hcmUI/CandidateExperience/en/sites/CX

Tenant: hcbt.fa.em2.oraclecloud.com  |  Site: CX. Same REST pattern as
Chubb/Hexaware/Icertis/WTW/eClerx already in this repo -- plain HTTP
requests, no Playwright needed, no auth. `data-sitenumber="CX"` is confirmed
directly in the site's own bootstrap HTML (`<base ... data-sitenumber="CX"
.../>`), and the REST query below echoes `"SiteNumber": "CX"` back
correctly, confirming the site number is right.

**Keyword is REQUIRED on this tenant** (same quirk as the RippleHire-backed
HDFC Bank/Axis Bank fetchers in this batch): an EMPTY `keyword=""` genuinely
returns `"TotalJobsCount": 0` for the entire global board -- this is NOT the
same thing as the board being empty. A real keyword (confirmed live
2026-09-06: "engineer"=121, "software engineer"=32, "senior software
engineer"=22, ".net developer"=369, "c# developer"=1, "angular"=1, "ai
engineer"=8, "machine learning engineer"=12, "python developer"=15,
"generative ai engineer"=19, a nonsense token `zzznonsensequeryabc123`=0)
returns plenty of real results, so `fetch_jobs()` here — like every other
company in this repo's pipeline — is always called with one of the
configured non-empty keywords, never with an empty string.

**Signal-to-noise**: this is a single bank-wide Oracle CE tenant, not a
tech-only board, and the keyword search is loose/OR-based across tokens the
same way RippleHire's is (e.g. "software engineer" and "ai engineer" both
also surface unrelated "Elite Banker"/"Platinum Relationship Manager"
branch-sales reqs) -- over-inclusive, not under-inclusive, which is safe
since matcher.py's own title/skill filters do the real precision work
downstream. Across a ~163-job India sample built from the standard keyword
list, roughly 38 are genuinely tech-titled: "Software Engineering II"
(SUPPORT SERVICES / CTO / Applications / CTB, Bangalore/Karnataka), "Dev Ops
Engineering I/II" (incl. an "AI & Platforms" team), "Software Developer
Engineer 1/2" and "... in Test 2" (a large Digital Banking "Kotak 811" app
engineering bench, Bangalore/Mumbai), "Software Test Engineering II",
"Penetration Tester", "Data Science I/II", "Data Scientist 2", "DevOps 2",
"Software Product Management III", "Tech Ops Engineering II" -- a real and
fairly sizable India tech-engineering presence, concentrated in Bangalore/
Karnataka with a secondary Mumbai/Hyderabad presence. The remaining ~125 are
retail branch-banking/relationship-manager/wealth roles across dozens of
Indian cities.

Location: `PrimaryLocation` already contains the literal substring "india"
for every posting observed (e.g. "Bangalore, Karnataka, India", "Mumbai,
Maharashtra, India", "Karnataka, India") -- no city-whitelist normalization
hack is needed here, unlike several other fetchers in this repo.

**Known gap, documented rather than silently worked around**: Kotak's own
"Digital/IT Jobs" marketing page separately lists roughly a dozen named tech
profiles (e.g. automation-qa, business-analyst, engineering-leader,
fullstackdeveloper, headofdatatechnology, java-developer, project-lead,
projectmanager, salesforce-developer, senior-reactjs, sfdc-developer,
softwaredeveloper) as static links to a generic "career-apply-now.html?
profile=<slug>" contact form -- no job ID, no posting date, no per-role
description beyond generic marketing copy, and no listing/detail REST
endpoint of its own. This is structurally incompatible with this repo's
per-job id/title/description/date contract, so it is deliberately NOT
scraped here; the real, paginated, ID-bearing job pool lives entirely in
the Oracle CE board this fetcher already queries.
"""
from __future__ import annotations

import re
import time

import requests

_BASE_URL = "https://hcbt.fa.em2.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX/job"

_SITE_NUMBER = "CX"
_PAGE_SIZE = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "ora-irc-language": "US",
    "Referer": f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX/jobs",
}

# Pagination-guard state: offset-0 first job IDs, per this process run.
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Oracle Cloud."""


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
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Kotak Mahindra Bank India job postings.

    Fetches globally (no location facet used, same conservative approach as
    Hexaware/Icertis/WTW/eClerx) and filters by "india" substring in
    PrimaryLocation client-side. `keyword` genuinely narrows results
    server-side; an empty keyword returns zero results on this tenant (see
    module docstring), so it is guarded here the same way as the
    RippleHire-backed fetchers in this batch.
    """
    global _FIRST_PAGE_IDS

    if not keyword:
        return []

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

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Kotak Mahindra Bank: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Kotak Mahindra Bank search failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Kotak Mahindra Bank fetch: no response — {last_exc}")

    data = r.json()
    items = data.get("items", [])
    if not items:
        return []

    req_list = items[0].get("requisitionList", [])

    jobs: list[dict] = []
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
            "title": (j.get("Title") or "").strip(),
            "location": location_str,
            "posting_date": posted_date,
            "application_url": f"{_JOB_BASE}/{job_id}",
        })

    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and jobs and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []  # wraparound detected

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    job_id = application_url.rstrip("/").split("/")[-1]

    finder = f'ById;Id="{job_id}",siteNumber={_SITE_NUMBER}'
    params = {
        "expand": "all",
        "onlyData": "true",
        "finder": finder,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_DETAIL_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Kotak Mahindra Bank description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Kotak Mahindra Bank description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Kotak Mahindra Bank description fetch: no response — {last_exc}")

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
