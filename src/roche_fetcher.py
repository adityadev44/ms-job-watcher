"""Fetches Roche job listings via the Workday public REST API.

Roche's ATS is Workday, hosted at roche.wd3.myworkdayjobs.com (tenant
"roche", site "roche-ext" -- confirmed live 2026-09-13 via a direct POST
to /wday/cxs/roche/roche-ext/jobs, which returned real jobPostings (HTTP
200, total=1289 globally). Found directly from a live web search turning
up the real `roche.wd3.myworkdayjobs.com/en-US/roche-ext` search URL, not
guessed from tenant-naming convention.

Same shape as J&J's tenant (not Novartis's): this tenant's only nested
facet under `locationMainGroup` is a flat `locations` list of ~233
individual city/region values with NO country-level facet at all. Of the
7 raw entries containing "india"/known-India-city-name text, 1 is a false
positive ("Indianapolis") and must be excluded, leaving 6 genuine India
location WIDs: a bare "India" facet value plus Bangalore, Chennai,
Hyderabad, Mumbai, and Pune. Applying all 6 as `appliedFacets.locations`
narrowed the global 1289-job pool to a verified India-only 50-job pool
(every returned `locationsText` value spot-checked -- Bangalore/Chennai/
Hyderabad/Mumbai/Pune/"2 Locations"/"3 Locations"/"4 Locations", no
non-India leakage).

Confirmed live 2026-09-13: of the 50 India postings, a real, substantial
software/AI engineering GCC presence exists in Hyderabad -- "AI Solution
Architect" (x2), "Fullstack Data Engineer - Applied & Agentic AI Systems"
(x2), "Fullstack AI Quality Engineer - Applied & Agentic AI Systems" (x2),
"Senior SAP Cloud Engineer", "IT Software Engineering Expert - RDT Pharma
Medicines", "Software Developer Regulatory Affairs and China Clinical
Development", "Regulatory AI & Automation Specialist" -- genuinely
AI/LLM-flavored engineering roles, not just commercial/regulatory/finance.
`searchText` provides fuzzy relevance-ranked narrowing rather than a
strict full-text filter (total=21 for "software engineer" vs 50
unfiltered, some noise at the top of the ranking -- e.g. an unrelated
"P&C Business Partner" title surfaced alongside genuine engineering
matches for a broader query) -- still a real narrowing effect, so this
fetcher is not added to `_IGNORES_KEYWORDS`; matcher.py's own
title/skill/exclude filters are the real precision layer regardless.

`locationsText` on this tenant is bare (e.g. "Hyderabad", "Pune",
"Mumbai") or a multi-site rollup ("2 Locations", "3 Locations", "4
Locations") -- never containing the literal "India" substring except for
the one bare "India" facet value itself. ", India" is appended
client-side when missing, safe because results are already pre-filtered
by the 6 India-only location WIDs above (same pattern as
Novartis/Fidelity/Pfizer/Maersk's "N Locations" handling) -- a job cannot
appear in this result set without matching at least one genuine India
city/region WID.

Two Workday quirks confirmed live on this tenant:
- Page size is capped at 20 -- limit=25 returns a raw HTTP 400 (same class
  as Northern Trust/Pfizer/Novartis/J&J's cap).
- Pagination was tested through the real last page (offset=40 of 50, then
  offset=60) and correctly returned an empty `jobPostings` array with no
  wraparound -- the page1-first-ID memo guard is still applied
  defensively, matching every other Workday fetcher in this repo.

Job detail descriptions come from the same Workday CXS JSON detail API
shape as Novartis/J&J/Pfizer: GET
.../wday/cxs/roche/roche-ext{externalPath} returns a
`jobPostingInfo.jobDescription` HTML blob and an already-ISO
`jobPostingInfo.startDate` (confirmed live: "2026-09-13").
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://roche.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/roche/roche-ext/jobs"
_JOB_BASE = f"{_BASE_URL}/roche-ext"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/roche/roche-ext"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# Genuine India location facet WIDs under the flat `locations` facet --
# this tenant has no country-level facet to apply instead. Excludes the
# "Indianapolis" false-positive that also matched a naive substring search
# over the raw facet list.
_INDIA_LOCATION_WIDS = [
    "763fba0474e70100f99cc4b76b650000",  # Bangalore
    "54c59631019f01c1589e94d7e67743a3",  # Chennai
    "4ae5b8b977ee0100f9037784a2ce0000",  # Hyderabad
    "54c59631019f01c479dfb787a377c235",  # India (bare)
    "54c59631019f011278ea85d7e67739a3",  # Mumbai
    "54c59631019f017ed6337bd7e6772fa3",  # Pune
]

_page1_first_id: dict[str, str] = {}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/roche-ext",
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
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
        "limit": min(num, _MAX_LIMIT),
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
                raise RateLimitError("Roche Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Roche fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    if postings:
        first_bullets = postings[0].get("bulletFields", [])
        first_id = first_bullets[0].strip() if first_bullets else ""
        if start == 0:
            if first_id:
                _page1_first_id[keyword] = first_id
        elif first_id and _page1_first_id.get(keyword) == first_id:
            return []

    jobs: list[dict] = []
    for p in postings:
        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the location-WID facet above --
        # bare city names ("Hyderabad", "Pune") and multi-site rollups
        # ("2 Locations") never say "India" on their own here.
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

        title = p.get("title", "").strip()
        if not title:
            continue

        external_path = p.get("externalPath", "")

        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets and bullets[0].strip() else external_path
        if not job_id:
            continue

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

    Returns (description_text, posting_date).
    The startDate field in the detail response is already ISO (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/roche-ext/", 1)[-1]
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
                raise RateLimitError("Roche description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Roche description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
