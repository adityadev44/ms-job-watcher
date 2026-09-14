"""Fetches Ciena job listings via the Workday public REST API.

Ciena's ATS is Workday, hosted at ciena.wd5.myworkdayjobs.com (tenant
"ciena", site "Careers"). Confirmed live 2026-09-14: POST to
/wday/cxs/ciena/Careers/jobs returns real jobPostings, apply pages are
branded "Careers at Ciena".

India is filtered server-side via the `Location_Country` facet (capitalised
key, same non-standard convention as Marsh McLennan/State Street, NOT the
lowercase `locationCountry` most Workday tenants use) with WID
c4f78be1a8f14da0ab49ce1162348a5e -- the same global "India" country
reference GUID reused across many Workday tenants in this repo (Shell,
Fidelity, Citi, Northern Trust, MUFG, ...). Verified: this facet returns
exactly 6 of the ~138 global jobs, and each returned job's locationsText
(Pune, Gurugram, "India-Gurgaon-TRIL Tower 4") is a genuine India location --
no leakage observed.

Ciena is a smaller networking vendor and its India R&D footprint (Gurugram +
a Pune design-tools presence) is genuinely small right now -- only 6 open
India postings total as of 2026-09-14 ("Senior UX Designer", "Kinaxis
Analyst", "Project Management", "ASIC/FPGA Design Engineer", "Product
Security Lab Engineer", "Technical Documentation Engineer- Python
Automation"). None of these titles currently match `title_family`'s
software-engineer phrase list -- 0 matches expected today, same "genuinely
small/zero right now, not a fetcher defect" disposition as GE Aerospace/
Genpact/Icertis in this repo. Feasible and onboarded anyway since the
integration itself works cleanly and Ciena does hire India SDE/AI roles
periodically (job titles here churn - a "Cloud Platform & AI Engineer"
India-Gurugram req was observed live on the branded apply-link search
results only days before this integration, already closed by the time of
onboarding).

Max page size / relative-date parsing / "2 Locations" client-side ", India"
append all follow the exact same pattern as shell_fetcher.py (same ATS
family, same defensive limit=20 cap and locationsText handling).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://ciena.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/ciena/Careers/jobs"
_JOB_BASE = f"{_BASE_URL}/Careers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/ciena/Careers"
_PAGE_SIZE = 20

# India Location_Country WID -- stable Workday GUID used across tenants.
# Verified 2026-09-14: returns 6 India results for an empty keyword search.
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
    "Referer": f"{_BASE_URL}/Careers",
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
    capped_num = min(num, _PAGE_SIZE)

    body = {
        "appliedFacets": {"Location_Country": [_INDIA_WID]},
        "limit": capped_num,
        "offset": start,
        "searchText": keyword,
    }

    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    json=body,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Ciena Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Ciena fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the Location_Country facet --
        # make sure the literal substring is present for matcher.py's India
        # check. "2 Locations" / bare office-code entries are multi-site
        # India postings; set to "India".
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc and loc != "2 Locations" else "India"

        external_path = p.get("externalPath", "")
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
    """Fetch job description via the Workday CXS JSON detail API.

    Transforms the HTML application URL to the JSON API path and returns
    (description_text, posting_date).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/Careers/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    api_url,
                    headers=_HEADERS,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                raise RateLimitError("Ciena description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Ciena description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )

    # startDate is already YYYY-MM-DD from the API
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
