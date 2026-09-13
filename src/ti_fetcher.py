"""
Texas Instruments job fetcher — Oracle HCM Cloud Candidate Experience REST API.

ATS identification (Step 1, verified live 2026-09-13, not guessed):
careers.ti.com/search-jobs redirects to
careers.ti.com/en/sites/CX/jobs, whose page HTML embeds
``edbz.fa.us2.oraclecloud.com`` as the underlying Oracle HCM tenant host —
the same REST API family already used in this repo by Chubb/Amex/Icertis/
Hexaware/BNY Mellon/eClerx/WTW/Goldman Sachs/JPMorgan (``chubb_fetcher.py``
is the closest existing template, same endpoint shapes, different tenant
host/site number: ``edbz.fa.us2.oraclecloud.com``, site ``CX``).

Unlike Chubb, this tenant DOES have a working, genuinely-narrowing India
location facet: ``selectedLocationsFacet=300000000361484`` (discovered from
the response's own ``locationsFacet`` block, not guessed) narrows the
698-job global pool to 104 India jobs, all in Bengaluru, Karnataka —
confirmed every returned ``PrimaryLocation`` genuinely names India, no
leakage. ``limit`` up to 100 works cleanly in one request (no 20-cap issue
like the Workday tenants in this same onboarding batch).

TI's Bengaluru GCC (104 jobs) is, as expected for a chip-design/analog
semiconductor company, overwhelmingly IC design/verification/layout/tapeout/
DFT/program-management hardware roles — but a handful of software-adjacent
titles do exist alongside it: "Application Developer" (title_family match,
but its JD is a pure SAP Basis administration role with no hard
``primary_skills`` term), "Public Cloud Engineer" (title_family match via
"cloud engineer", but its JD names no hard skill term either), "Solutions
Architect" / "Integration Architect" (excluded from `title_family` — no
"architect" phrase covered, same precision gap flagged for Broadcom in this
same onboarding batch), and "Senior Data Scientist" (title_family does not
currently include a bare "data scientist" phrase). 0 current matches is
therefore expected and genuine — same "confirmed-low-volume-is-real" class
as Micron/Intel/Genpact in this repo, not a fetcher defect. The pipeline is
wired correctly and will alert the moment a qualifying posting appears.
"""
from __future__ import annotations

import re
import time

import requests

_BASE_URL = "https://edbz.fa.us2.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX/job"

_SITE_NUMBER = "CX"
_INDIA_LOCATION_FACET_ID = 300000000361484
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
    # This tenant's finder syntax breaks (returns 0 jobs, not the unfiltered
    # pool) when a literal `keyword="",` clause is present — unlike Chubb's
    # tenant, an empty-but-present keyword clause is not a no-op here. Omit
    # the clause entirely when there is no keyword instead of passing "".
    keyword_clause = f'keyword="{keyword}",' if keyword else ""
    finder = (
        f"findReqs;siteNumber={_SITE_NUMBER},"
        f"facetsList=LOCATIONS,"
        f"limit={num},"
        f"offset={start},"
        f"{keyword_clause}"
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
                raise RateLimitError(f"TI search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    data = r.json()
    items = data.get("items", [])
    if not items:
        return []

    search_obj = items[0]
    req_list = search_obj.get("requisitionList", [])

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
