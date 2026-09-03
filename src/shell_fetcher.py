"""Fetches Shell job listings via the Workday public REST API.

Shell's ATS is Workday, hosted at shell.wd3.myworkdayjobs.com (tenant
"shell", site "ShellCareers"). Confirmed live 2026-09-03: POST to
/wday/cxs/shell/ShellCareers/jobs returns real jobPostings, and every job's
apply page is branded "Work at Shell" / "Shell.com" -- this is the real
energy company, not a same-named unrelated tenant (see PLAYBOOK's
Macquarie/Macquarie-University lesson for why that check matters).

India is filtered server-side via the locationCountry facet WID
c4f78be1a8f14da0ab49ce1162348a5e -- the same global "India" country reference
ID reused across many Workday tenants in this repo (Wells Fargo, Fidelity,
Citi, Northern Trust, MUFG, ...). Facet spot-checked against every returned
job's locationsText (Chennai/Bangalore/Mumbai office names only) -- unlike
Micron/Verizon/Lowe's, this tenant's facet shows no non-India leakage.

Max page size is 20 (limit=25+ silently returns an empty jobPostings/no
`total` field at all, not an HTTP error -- same failure shape as Northern
Trust's limit=25+ HTTP 400, just silent here). Pagination via offset.

locationsText is always a bare office/city name ("Bangalore RMZ-ECO WORLD",
"SHELL CENTRE - CHENNAI", "Mumbai - BG House") or "2 Locations" for
multi-site postings -- never contains the word "India" -- so ", India" is
appended client-side (safe: the facet already guarantees India-only results),
same fix as Northern Trust/Maersk's "2 Locations" case.

Shell's Bengaluru/Chennai centres ("Shell Technology Centre - Bangalore",
"capability centres across Bangalore and Chennai") do real digital/data/cloud
engineering for the energy business alongside heavy finance/supply-chain/
trading roles -- current pool is small (~33 India postings total, most
Chennai/finance-analyst titles that never reach title_family). Confirmed one
genuine live match: "Software Engineer ENDUR" (Bangalore, R209670) explicitly
names "developing solutions using Java and .Net" in its JD body -- a real
`.NET / C#` tag despite the title giving no tech signal itself.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://shell.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/shell/ShellCareers/jobs"
_JOB_BASE = f"{_BASE_URL}/ShellCareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/shell/ShellCareers"
_PAGE_SIZE = 20

# India locationCountry WID -- stable Workday GUID used across tenants.
# Verified 2026-09-03: returns 33 India results for an empty keyword search,
# matching the count reported in the tenant's own locationCountry facet list.
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
    "Referer": f"{_BASE_URL}/ShellCareers",
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
    # Workday silently returns an empty page for limit > 20 on this tenant;
    # cap defensively instead of losing results with no visible error.
    capped_num = min(num, _PAGE_SIZE)

    body = {
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
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
                raise RateLimitError("Shell Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Shell fetch failed: {exc}") from exc

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
        # Already pre-filtered to India via the locationCountry facet -- make
        # sure the literal substring is present for matcher.py's india check.
        # "2 Locations" entries are multi-site India postings; set to "India".
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
        ext_path = "/" + application_url.split("/ShellCareers/", 1)[-1]
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
                raise RateLimitError("Shell description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Shell description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )

    # startDate is already YYYY-MM-DD from the API
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
