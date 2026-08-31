"""Fetches ING (ING Group) job listings via the Workday public REST API.

ING's ATS is Workday, hosted at ing.wd3.myworkdayjobs.com (tenant "ing").
careers.ing.com is a Radancy/TalentBrew frontend skin (same pattern as
Barclays' search.jobs.barclays skin) -- the actual job data lives on the
Workday tenant. The site name is NOT any of the obvious guesses ("ING",
"External", "ING_Careers", etc. all 404) -- it's "ICSGBLCOR" ("ICS" =
International Careers Site, "GBL COR" = Global Corporate), discovered via
web search of real ing.wd3.myworkdayjobs.com job URLs and confirmed live
2026-08-30: POST to /wday/cxs/ing/ICSGBLCOR/jobs returns real jobPostings
(734 total unfiltered). A second, much smaller site "ICSNLDGEN" also
exists (Netherlands-only general site, ~6 jobs) but ICSGBLCOR is ING's
global site and covers every location including any India postings.

ICSGBLCOR exposes no country-level location facet -- only a flat
"locationMainGroup" -> "locations" facet listing ~99 individual offices
(Amsterdam, Brussels, Manila, Bucharest, Katowice, Singapore, etc.). India
is NOT among them: a full unfiltered fetch of all 734 open jobs (2026-08-30)
found zero with an India-matching locationsText, and searching "Bangalore"
returns 0 results. Searching "India" as a keyword returns 12 jobs, but all
are located elsewhere (Brussels/Manila/Singapore/Seoul/Tokyo/Amsterdam) --
they merely mention India in the JD body (e.g. APAC coverage roles), not
genuine India postings. ING currently appears to have no live openings in
India on this Workday tenant (their India presence, if any, is not routed
through this ATS) -- this is expected per the task brief and mirrors the
"0 matches expected" situations already documented for BNY/GM/PayPal.

Since there's no location facet to apply and no known India office WIDs to
target, this fetcher follows the "no facet" pattern used by PayPal/FactSet/
Fiserv: fetch globally per keyword (searchText IS honored server-side on
this tenant -- verified live totals differ per keyword, e.g. "software
engineer" -> 159, "developer" -> 73, unlike Deutsche Bank/Genpact where
keywords are ignored) and filter India client-side with a word-boundary
regex so "Indianapolis"/"Indiana" can never slip through if ING ever opens
an India office job whose city name happens to overlap.

Job IDs come from bulletFields as "REQ-XXXXXXX" strings (no prefix
transformation needed, unlike Barclays' "JR-" normalization). Workday
rejects limit > 20 on this tenant (HTTP 400), same cap as Northern Trust.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://ing.wd3.myworkdayjobs.com"
_SITE = "ICSGBLCOR"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/ing/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/ing/{_SITE}"

_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/{_SITE}",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper -- Workday returns relative strings like "Posted 3 Days Ago"
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

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
        "appliedFacets": {},
        "limit": num,
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
                raise RateLimitError("ING Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ING fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = p.get("locationsText", "").strip()
        # No location facet on this tenant -- global fetch, India filtered
        # client-side. Word-boundary match so "Indianapolis"/"Indiana" (and
        # any office name that happens to contain "ind") never pass.
        if not re.search(r"\bindia\b", loc.lower()):
            continue

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
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
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
                raise RateLimitError("ING description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"ING description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
