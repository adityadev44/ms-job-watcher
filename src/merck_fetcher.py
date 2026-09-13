"""Fetches Merck & Co./MSD job listings via the Workday public REST API.

Disambiguation note (important): "Merck" outside the US/Canada usually
refers to Merck KGaA (careers.merckgroup.com, a completely separate German
company), while the US-based Merck & Co. trades and hires internationally
as "MSD" (jobs.msd.com). Investigated both live on 2026-09-13:

- careers.merckgroup.com (Merck KGaA): Phenom People frontend referencing
  SuccessFactors -- not investigated further once MSD's GCC pipeline
  confirmed real SDE/AI hiring, per the task's guidance to prefer whichever
  entity actually has India tech hiring.
- jobs.msd.com (Merck & Co. / MSD): also a Phenom People frontend
  ("phApp.ddo" SSR blob present, same shape as GE HealthCare/GE Aerospace/
  MSD's sibling GCC integrations), but the real search/apply backend is
  Workday -- confirmed via the page's own sign-in link pointing at
  msd.wd5.myworkdayjobs.com/SearchJobs. A direct POST to
  /wday/cxs/msd/SearchJobs/jobs returns real data (HTTP 200, total=1171
  global jobs) -- "SearchJobs" is the site code; the more guessable "MSD"/
  "MSDCareers"/"MSDJobs"/"MSD_Careers" all 404 with errorCode S21, the same
  "URL/tenant guess needs independent site-code discovery" lesson as
  Novartis.

This tenant has no flat locationCountry/Location_Country facet -- like
Novartis/Nvidia, the only location facet is nested (`locationMainGroup` ->
inner `locations`). Unlike Novartis's inner facet (a `locationCountry`
descriptor that can be applied directly), MSD's inner facet is a full
city-level "locations" list with no separate country rollup entry that
covers every India office consistently -- instead we hardcode every
current India location WID found in that facet response (Hyderabad HITEC
City Raidurg [the primary GCC tech campus, largest bucket], plus Mumbai,
Pune, Bangalore, Gurgaon, and Vizag office WIDs) in `appliedFacets.locations`.
Confirmed live: applying these 10 WIDs narrows total=1171 (global) to
total=58 (India) -- and manually re-checked every returned `locationsText`
value to confirm zero non-India leakage.

Confirmed live 2026-09-13: MSD's Hyderabad Tech Center is a genuine,
active software/AI engineering GCC -- current postings include "Specialist,
AI Engineering" (JD explicitly names GenAI, prompt engineering, RAG
pipelines, LLM-based workflows), "Specialist Software Engineering,
Full-Stack", "Senior Specialist, Senior Azure Full-Stack Developer", and
"Senior Specialist, Software Engineering" -- this is not just a pharma R&D
board, real SDE/AI-engineer titles are present today. The "Specialist,
X Engineering" title convention (not literally "Software Engineer") still
passes `title_family`'s substring match ("software engineering" contains
"software engineer"; "ai engineering" contains "ai engineer").

Two Workday quirks confirmed live on this tenant, both guarded here:
- Page size is capped at 20 -- limit=25/50 both return a raw HTTP 400
  (same class as Northern Trust/Pfizer/Novartis).
- Unlike Novartis/UBS/MUFG/Nvidia/Pfizer/Walmart, pagination on this
  tenant does NOT wrap around past the true total -- offset >= total
  (58) correctly returns an empty jobPostings array with the same total
  field. No page1-first-ID memo guard needed, but the fetcher still
  treats an empty postings page as a clean stop signal.

locationsText is already India-scoped by the facet (never a bare state/
city name without "IND -" prefix) but doesn't contain the literal string
"India" -- either an "IND - <state> - <city>" descriptor or an ambiguous
"2 Locations" rollup (Novartis/Maersk pattern). ", India" is appended
client-side when the substring is missing, safe because results are
already pre-filtered server-side by the hardcoded India WIDs.

Job detail descriptions use the same Workday CXS JSON detail API shape as
Novartis/Pfizer: GET .../wday/cxs/msd/SearchJobs{externalPath} returns a
`jobPostingInfo.jobDescription` HTML blob and an already-ISO
`jobPostingInfo.startDate`.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://msd.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/msd/SearchJobs/jobs"
_JOB_BASE = f"{_BASE_URL}/SearchJobs"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/msd/SearchJobs"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# Every India location WID observed in the tenant's nested
# locationMainGroup -> locations facet as of 2026-09-13. No single "IND -
# India - India" country rollup covers every office consistently (there IS
# one such entry, included below, but most postings use a specific office
# WID instead) -- so every distinct India office WID is listed explicitly,
# same defensive approach as Barclays' 11 India city WIDs.
_INDIA_WIDS = [
    "7ecbbff47ce1013ab665ac01e4216ce4",  # IND - Andhra Pradesh - Vizag
    "e3840447b75b015e4e5f2791ed2a20f8",  # IND - Haryana - Gurgaon (ANTELLIQ)
    "7ecbbff47ce101de70639e01e42135e4",  # IND - India - India (rollup)
    "65e8cd1027c3016347f728adf7014300",  # IND - Karnataka - Bangalore (Distribution)
    "c03b3c88494201c9d609073faf01ed3e",  # IND - Maharashtra - Mumbai (WeWork)
    "9721dcc9b11e010114bb8e9bf11e0000",  # IND - Maharashtra - Pune (Wework)
    "d85bc048731b012a033bbe22f801121e",  # IND - Telangana - Hyderabad
    "7ecbbff47ce1019a4721d401e421f3e4",  # IND - Telangana - Hyderabad
    "cce98a0a6ac6100205520b9330650000",  # IND - Telangana - Hyderabad (HITEC City)
    "4784d3113bd610018dc0d8d50a4e0000",  # IND - Telangana - Hyderabad (Hitec City Raidurg)
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/SearchJobs",
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
        "appliedFacets": {"locations": _INDIA_WIDS},
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
                raise RateLimitError("MSD Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"MSD fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    jobs: list[dict] = []
    for p in postings:
        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the hardcoded location WIDs --
        # make sure the literal substring is present for matcher.py's india
        # check. "IND - <state> - <city>" descriptors and "N Locations"
        # rollups never say "India" on their own.
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

    Returns (description_text, posting_date). startDate in the detail
    response is already ISO (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/SearchJobs/", 1)[-1]
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
                raise RateLimitError("MSD description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"MSD description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
