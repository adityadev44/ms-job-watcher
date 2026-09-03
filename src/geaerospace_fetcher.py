"""Fetches GE Aerospace job listings via the Workday public REST API.

Assignment for this fetcher was "John F. Welch Technology Center (JFWTC),
Bengaluru" with prior secondary research guessing the ATS as "custom" --
per PLAYBOOK.md's Wave 5 lesson ("ATS: custom" labels that never opened
DevTools should be treated as unverified), that guess was re-checked from
scratch and was wrong.

The public-facing careers.geaerospace.com frontend IS Phenom People (same
CDN family as Morningstar/United Airlines -- `phApp.ddo` SSR blob, refNum
"GAOGAYGLOBAL") -- but every job record embedded in that SSR blob carries
its own `applyUrl` pointing at `geaerospace.wd5.myworkdayjobs.com`, exposing
the real backend ATS: Workday, tenant "geaerospace", site "GE_ExternalSite"
(confirmed live 2026-09-03 via `POST /wday/cxs/geaerospace/GE_ExternalSite/
jobs` returning real jobPostings -- same "Phenom skin over a different real
ATS" shape as Boeing/TalentBrew-over-Workday in Wave 5, just Phenom instead
of TalentBrew as the skin this time). The Phenom frontend's own `location=`
text param is unusable here (confirmed: any free-text `location`/`country`
value collapses results to 0 hits -- Phenom's geocoding needs a resolved
place_id this repo can't produce without a browser), so this fetcher talks
to the Workday CXS API directly and never touches the Phenom layer at all.

No `locationCountry`/`Location_Country` facet exists on this tenant -- the
only location facet Workday exposes is a flat city-level `locations` list
under `locationMainGroup` (confirmed by inspecting the unfiltered facets
response; no second "country" sibling group the way Nvidia's tenant has).
India presence found by scanning all ~483 global jobPostings for genuine
India place names (not the "India" substring -- that would also match
"Indianapolis", a real Workday US site here, the same false-positive class
PLAYBOOK.md documents for PayPal/FactSet): exactly two India cities, with
three total facet IDs (Bengaluru has two distinct site records):
    Bengaluru: 74cbdbfadb6e10014faaf4f6cfe90000
    Bengaluru: f6f2e0e96f41100144c2a8d14bd00000
    Pune:      74cbdbfadb6e10014fad7b09ac4a0000
Hardcoded and applied via `appliedFacets.locations`, same pattern as
Barclays' 11 India city WIDs. Pune is included deliberately even though
`exclude_locations` filters it out downstream -- keeps this fetcher's
India scope complete and lets the shared config own the exclusion policy,
consistent with the rest of the repo. NOTE: unlike tenants with a real
country facet, a brand-new India city opening up here will NOT be picked
up automatically -- it requires a code update to add the new facet ID
(same limitation Barclays already has).

Verified live 2026-09-03: this tenant's current entire India pool is just
9 jobs (2 Bengaluru + 7 Pune), none of which are software-engineering
titled -- Bengaluru has "Data Science Intern" (excluded by `exclude_terms`
"intern") and "Properties Manager" (non-tech); Pune's 7 are manufacturing/
quality/HR roles. Zero real `.NET/C#` or `AI/ML/Python` matches exist
right now. This is a genuine current fact, not a fetcher defect -- same
"confirmed-zero-is-real" class as Boeing/Pfizer/PepsiCo in Wave 5 and
ING/eClerx/AIG in earlier waves. `careers.geaerospace.com` itself (via
web search) surfaces JFWTC software-engineering postings ("Senior Software
Engineer - Geometry Tools", third-party-board-cached) that are simply not
live in the current search pool -- this tenant's requisitions appear to
churn fast, consistent with GE Aerospace's other-site posting cadence.

Two quirks confirmed and guarded here:
- `limit` > 20 returns a clean HTTP 400 (Northern Trust-style cap, not a
  silent no-op like Shell) -- clamped defensively via `_MAX_LIMIT`.
- Pagination does NOT wrap around: `offset` past a query's real total
  returns an empty `jobPostings` array with an accurate `total` field
  (confirmed at offset 500 against an unfiltered total of 483, and at
  offset 20 against a facet-filtered total of 9) -- unlike the UBS/MUFG/
  Nvidia/Pfizer/Walmart wraparound family, no first-ID watermark guard is
  needed on this tenant.

`searchText` genuinely narrows server-side (full-text over descriptions,
not just titles, verified by "software" -> 93/483 and "Geometry Tools" ->
8/483 returning different pools) -- do NOT add this slug to
_IGNORES_KEYWORDS.

Descriptions are NOT inline in the search response (`jobPostings` entries
carry only title/location/postedOn/externalPath) -- `fetch_job_description`
hits the standard Workday CXS job-detail endpoint (`GET /wday/cxs/
geaerospace/GE_ExternalSite{externalPath}`), same shape as every other
Workday fetcher in this repo. Its `startDate` field is already ISO
(YYYY-MM-DD) and cross-checked against the search response's relative
`postedOn` text on a live job (`startDate: 2026-08-28` matched "Posted 6
Days Ago" against a 2026-09-03 run) -- reliable, used as the authoritative
posting date like Pfizer.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://geaerospace.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/geaerospace/GE_ExternalSite/jobs"
_JOB_BASE = f"{_BASE_URL}/GE_ExternalSite"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/geaerospace/GE_ExternalSite"

_PAGE_SIZE = 20

# This tenant has no country-level location facet -- only a flat city list
# under locationMainGroup. Hardcoded India city WIDs (see module docstring
# for how these were found and verified).
_INDIA_LOCATION_WIDS = [
    "74cbdbfadb6e10014faaf4f6cfe90000",  # Bengaluru
    "f6f2e0e96f41100144c2a8d14bd00000",  # Bengaluru (second site record)
    "74cbdbfadb6e10014fad7b09ac4a0000",  # Pune
]

# limit > 20 returns a raw HTTP 400 on this tenant (Northern Trust-style
# cap). matcher.py always calls with num=20 so this is defensive.
_MAX_LIMIT = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/GE_ExternalSite",
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
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("GE Aerospace Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"GE Aerospace fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    jobs: list[dict] = []
    for p in postings:
        loc = p.get("locationsText", "").strip()
        # Already pre-filtered to India via the hardcoded city facets --
        # make sure the literal substring is present for matcher.py's India
        # check (plain city names like "Bengaluru"/"Pune" never say "India").
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
        ext_path = "/" + application_url.split("/GE_ExternalSite/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    api_url,
                    headers=_HEADERS,
                    timeout=timeout,
                )
            if r.status_code == 429:
                raise RateLimitError("GE Aerospace description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"GE Aerospace description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
