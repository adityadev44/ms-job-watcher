"""Fetches Lloyds Banking Group job listings via the Workday public REST API.

Lloyds Banking Group's ATS is Workday, hosted at lbg.wd3.myworkdayjobs.com
(tenant "lbg"). Two sites exist on this tenant:
  - "LBG_Careers" -- the general UK-wide careers site (~105 jobs, almost
    entirely UK locations).
  - "Lloyds_Technology_Centre" -- the dedicated India GCC portal for Lloyds
    Technology Centre, Hyderabad (~49 jobs). Confirmed live 2026-08-31: POST
    to /wday/cxs/lbg/Lloyds_Technology_Centre/jobs returns real jobPostings,
    every one located at either "Hyderabad Knowledge City (LTC)" or
    "Hyderabad Knowledge Park Tower 2" -- no country facet is needed since
    the site is India-only by construction (same pattern as First American's
    dedicated "faicareers" portal). This fetcher targets that India site.
    The detail API's `country` field independently confirms every posting as
    India (WID c4f78be1a8f14da0ab49ce1162348a5e, the same cross-tenant India
    GUID reused across Fidelity/Wells Fargo/Citi/Northern Trust/MUFG), and
    `hiringOrganization` reads "Lloyds Offshore Global Services Private
    Limited".

Quirk (same bug class as Accenture/Northern Trust/MUFG -- see PLAYBOOK):
this tenant HTTP 400s on any `limit` above 20 (tested live: 20 -> 200,
21/25/30/40/50 -> 400). fetch_jobs clamps `num` to 20 defensively. The
`total` field in the response also resets to 0 on every paginated request
(offset > 0) even though `jobPostings` still contains a full page of real
results -- identical to the documented Accenture/MUFG bug, so `total` is
never used as a pagination signal.

Pagination also wraps around past the real result count instead of
returning an empty page (same bug class as UBS BrassRing/MUFG): requesting
offset >= total (49) does NOT return an empty jobPostings array -- it
silently re-returns page 1 verbatim. Verified live: offset 60 replayed the
exact same 20 job IDs (163024, 163015, 163016, ...) as offset 0. Track each
keyword's first-page first job ID and treat a repeat of it on a later page
as "no more results", same fix as mufg_fetcher.py.

searchText is genuinely server-side but converges strangely on this small
pool -- e.g. "C#" returns all 49 jobs unfiltered (the "#" symbol appears to
break the query, same TCS iBegin lesson) while "Java"/"Python"/".NET" do
narrow the result count. Since the whole India pool is only ~49 jobs, this
doesn't matter in practice; matcher.py's own title/skill filtering handles
precision regardless of what searchText narrows.

Live content check (2026-08-31): searched all 49 India postings' full
descriptions for every require_tech_in_description-eligible term. Zero
postings mention any real .NET signal (C#, ASP.NET, dotnet, Web API, Entity
Framework) or any AI/ML hard term (LangChain, LangGraph, generative ai,
etc.) anywhere. 4 postings (Quality Engineer / Software Engineer roles) do
contain the primary_skills-only term "SQL Server" -- but only as one entry
in a generic "Common Tools: Databases: SQL Server, Oracle, Snowflake,
BigQuery" list for Pentaho/ETL data-testing work, not evidence of .NET
development. This is the same false-positive shape the playbook's Layer 4
rationale describes for other direct BFSI employers (Citi, State Street,
Adobe, ...), so `require_tech_in_description` is enabled for this company.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://lbg.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/lbg/Lloyds_Technology_Centre/jobs"
_JOB_BASE = f"{_BASE_URL}/Lloyds_Technology_Centre"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/lbg/Lloyds_Technology_Centre"

# Tenant HTTP 400s on any limit above 20 (verified live: 20 -> 200, 21 -> 400).
_PAGE_SIZE = 20
_MAX_LIMIT = 20

# Pagination wraps around past the real result count for a keyword instead of
# terminating with an empty page (same bug class as UBS BrassRing/MUFG):
# requesting offset >= total does NOT return an empty jobPostings array -- it
# silently re-returns page 1 verbatim, forever. Track each keyword's
# first-page first job ID and treat a repeat of it on a later page as "no
# more results" so matcher.py's `if not page: break` pagination loop
# actually terminates instead of re-fetching the same page until
# max_listings is exhausted.
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
    "Referer": f"{_BASE_URL}/Lloyds_Technology_Centre",
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
                raise RateLimitError("Lloyds Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Lloyds fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    # Detect the pagination wrap-around bug (see _page1_first_id comment
    # above) before doing any other work on this page.
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
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # This is Lloyds' dedicated India (Hyderabad) portal -- every result
        # is genuinely India, but locationsText is a facility name with no
        # "India" substring ("Hyderabad Knowledge City (LTC)"). Append it
        # client-side for matcher.py's is_india_job() check, same fix as
        # First American/Citi/Barclays/MUFG.
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

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

    Returns (description_text, posting_date).
    The startDate field in the detail response is already ISO format
    (YYYY-MM-DD), so no relative-date conversion is needed here.
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/Lloyds_Technology_Centre/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(3):
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
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Lloyds description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Lloyds description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
