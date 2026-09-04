"""Fetches Novartis job listings via the Workday public REST API.

Novartis's ATS is Workday, hosted at novartis.wd3.myworkdayjobs.com
(tenant "novartis", site "Novartis_Careers" -- confirmed live 2026-09-03
via a direct POST to /wday/cxs/novartis/Novartis_Careers/jobs, which
returned real jobPostings (HTTP 200, total=926 globally). The site name
was NOT guessable from the tenant alone -- an unscoped root-path GET on
novartis.wd3.myworkdayjobs.com returns a bare HTTP 406 with no body, and
guessing "Novartis-Careers" (hyphenated) 404s with errorCode "S21"
("not found: Job_Posting_Site_ID=Novartis-Careers"); the real site name
("Novartis_Careers", underscore) was found via a live web search turning
up real job-detail URLs on this tenant, not by pattern-matching other
companies' site-name conventions.

This tenant's facet response nests location facets one level deep, the
same general shape as Nvidia's locationMainGroup/locationHierarchy1
pattern -- Novartis's `facets` list is
[jobFamilyGroup, workerSubType, timeType, locationMainGroup], with
locationMainGroup itself containing a nested "locationCountry" facet
descriptor. Unlike Nvidia (where the *inner* key that actually works in
appliedFacets is a tenant-specific, non-obvious name), Novartis's nested
facet's own facetParameter is simply "locationCountry" -- and passing
{"locationCountry": [india_wid]} directly in the top-level appliedFacets
dict (the same flat shape used by Fidelity/Micron/Adobe/Kyndryl/etc.)
works correctly and narrows results server-side (confirmed: total=130
with the facet applied vs 926 without). The India WID is the same
cross-tenant GUID reused across Fidelity/Wells Fargo/Citi/Northern
Trust/MUFG/Target/Pfizer/Shell.

Two Workday quirks confirmed live on this tenant, both guarded here:
- Page size is capped at 20 -- limit=25 or limit=50 both return a raw
  HTTP 400 (same class as Northern Trust/Pfizer's cap).
- Pagination wraps around past the true total (same bug class as
  UBS/MUFG/Nvidia/Pfizer/Walmart): requesting offset >= total does NOT
  return an empty jobPostings array once past the genuine last page --
  it silently replays page 1's exact job set again. Confirmed live:
  offset=120 (of 130 total) correctly returned the real final 10 jobs
  with total=0 (Accenture's "total reverts to 0 on a real last page"
  quirk, also present here), but offset=130 replayed page 1's first job
  ID verbatim. Guarded via the same page1-first-ID memo pattern as
  Pfizer/MUFG/Nvidia/Walmart.

Unlike Pfizer, Novartis's keyword search (searchText) appears to be a
genuine full-text match against JD *body* content, not just the title --
confirmed live: searchText="generative ai" returns 43 India results with
generic-sounding titles ("Senior Specialist Platform Services", "Sourcing
Manager - IT & Digital") that don't contain those words themselves, the
same full-text-search shape documented for SAP Labs. This means
different default keywords legitimately return different (but
overlapping) result pools -- keywords are NOT ignored server-side here,
so this fetcher is not added to `_IGNORES_KEYWORDS`.

locationsText never contains the literal string "India" -- it's either a
bare city/office label ("Hyderabad (Office)", "Mumbai (Head Office)"), a
bare Indian state name ("Telangana", "Kerala", "Tamil Nadu"), or a
multi-site rollup ("2 Locations", "3 Locations"). ", India" is appended
client-side when missing, safe because results are already pre-filtered
by the India country facet server-side (same pattern as
Fidelity/Pfizer/Marsh McLennan). City/state names naming an excluded
region (Chandigarh, Tamil Nadu, Kerala) are deliberately left intact in
the appended string so config's `exclude_locations` substring check
still fires correctly downstream.

Job detail descriptions come from the same Workday CXS JSON detail API
shape as Pfizer: GET .../wday/cxs/novartis/Novartis_Careers{externalPath}
returns a `jobPostingInfo.jobDescription` HTML blob and an already-ISO
`jobPostingInfo.startDate` (confirmed live: "2026-09-02", no relative-date
parsing needed for this field -- unlike `postedOn` in the search response,
which does use Workday's usual relative-date strings like "Posted Today").

Confirmed live 2026-09-03: of the ~130 current India postings, this is a
real pharma GCC with a substantial Hyderabad tech/data/AI presence
(Data Engineering Manager, AI Infrastructure & Platform Engineering,
Data Science & AI specialist roles, multiple "generative ai"/"RAG"/"LLM"
hits in JD bodies via full-text keyword search) -- but see the fetcher's
companion note in the final onboarding report about title_family
coverage: none of the current postings' *titles* literally contain any
`matching.title_family` phrase (no bare "Software Engineer"/"Data
Engineer"/etc.; the real titles are level-banded as "Associate
Director"/"Director"/"Manager"/"Senior Specialist"/"Senior Analyst",
which are excluded outright by `exclude_terms`, or fall entirely outside
`title_family`'s phrase list otherwise) -- so 0 current title-family
matches is a real, current fact about this specific snapshot of postings,
not a fetcher defect.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://novartis.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/novartis/Novartis_Careers/jobs"
_JOB_BASE = f"{_BASE_URL}/Novartis_Careers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/novartis/Novartis_Careers"

_PAGE_SIZE = 20

# India country WID -- the standard cross-tenant Workday "India" reference ID,
# exposed on this tenant under the (nested, but directly usable) "locationCountry"
# facet key.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# This tenant caps page size at 20 -- requesting limit=25/50 returns a raw
# HTTP 400 (same class of cap as Northern Trust/Pfizer). matcher.py always
# calls with num=20 so this is defensive, not currently load-bearing.
_MAX_LIMIT = 20

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS BrassRing / MUFG / Nvidia / Pfizer / Walmart): requesting
# offset >= total does NOT return an empty jobPostings array -- it silently
# re-returns page 1 verbatim once past the genuine last page. Track each
# keyword's first-page first job ID and treat a repeat of it on a later page
# as "no more results".
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
    "Referer": f"{_BASE_URL}/Novartis_Careers",
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
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
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
                raise RateLimitError("Novartis Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Novartis fetch failed: {exc}") from exc

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
        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the country facet -- make sure the
        # literal substring is present for matcher.py's india check. City/state
        # names (e.g. "Hyderabad (Office)", "Tamil Nadu") and multi-site
        # rollups ("2 Locations") never say "India" on their own here.
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
        ext_path = "/" + application_url.split("/Novartis_Careers/", 1)[-1]
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
                raise RateLimitError("Novartis description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Novartis description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
