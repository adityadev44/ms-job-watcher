"""Fetches Nvidia job listings via the Workday public REST API.

Nvidia's ATS is Workday, hosted at nvidia.wd5.myworkdayjobs.com (tenant
"nvidia", site "NVIDIAExternalCareerSite"). Confirmed live 2026-09-03:
POST to /wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs returns real
jobPostings, and the site's own apply flow (nvidia.wd5.myworkdayjobs.com/
NVIDIAExternalCareerSite) links directly into this tenant.

India is filtered server-side, but NOT via the usual flat "locationCountry"
facet key (Fidelity/Wells Fargo/Northern Trust) nor "Country_and_Jurisdiction"
(Citi) nor capitalised "Location_Country" (MMC/State Street/eBay/Target).
This tenant nests country under a two-level "locationMainGroup" facet group:
the country-level values live under the *inner* facetParameter
"locationHierarchy1" (a sibling "locationHierarchy2" holds Office/Remote, and
a third sibling "locations" holds individual city "Sites"). Discovered by
requesting a large, unfiltered result set and inspecting the nested facets
list in the response body — the outer "locationMainGroup" key itself is not
a usable appliedFacets key. India's WID under "locationHierarchy1" is
2fcb99c455831013ea52b82135ba3266 (tenant-specific — NOT the shared
cross-tenant India GUID c4f78be1a8f14da0ab49ce1162348a5e seen at
Fidelity/Wells Fargo/Citi/Northern Trust/MUFG/Accenture). Applying it returns
236 India jobs (~2000 global jobs capped total).

Keyword search (searchText) IS genuinely server-side here (confirmed: an
empty query returns 236, "Software Engineer" narrows to 125, a nonsense
string returns 0) -- unlike many other tenants in this repo, do not add this
slug to _IGNORES_KEYWORDS.

Quirk (same bug class as MUFG): this tenant HTTP 400s on any `limit` above
20 (tested: 20 -> 200 OK, 21/50 -> 400). fetch_jobs clamps `num` to 20.

Quirk (same bug class as UBS BrassRing / MUFG): pagination wraps around
past the real result count for a keyword instead of returning an empty
page. Verified live for the default empty-keyword query (total=236):
offset=220 correctly returns the last partial page (16 jobs), but
offset=236/240/256 all silently replay the exact same 20 page-1 job IDs
forever (with `total` back to its correct non-zero value, so `total` can't
be used to detect this either). Same fix as MUFG: memoize each keyword's
page-1 first job ID and treat a repeat of it on a later page as "no more
results" so matcher.py's `if not page: break` pagination loop actually
terminates.

Most India postings' locationsText is already a literal "India, <City>"
string (e.g. "India, Bengaluru"), but multi-site postings collapse to
"2 Locations"/"3 Locations"/etc. with no country text at all. Since the
locationHierarchy1 facet already guarantees every result is genuinely
India, ", India" is appended client-side when the substring is absent --
same fix as Fidelity/MUFG/Citi/Barclays/Maersk.

Real matches confirmed on both tracks: AI/ML/Python is strong (e.g. "Senior
AI/ML Software Engineer" in Bengaluru explicitly names LangChain, LangGraph,
RAG, LLMs, and vector databases in its JD body -- a genuine hard match, not
just a title coincidence). .NET/C# volume is comparatively low -- Nvidia is
a GPU/systems/AI company, not a .NET shop -- but the keyword searches for
".NET"/"C#" mostly surface false-positive substring hits ("Networking",
generic "Engineer" titles) rather than real .NET postings; real .NET/C#
matches, if any, will come through the shared title/skill filters on
whatever the description body actually says, same as everywhere else.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://nvidia.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
_JOB_BASE = f"{_BASE_URL}/NVIDIAExternalCareerSite"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/nvidia/NVIDIAExternalCareerSite"

# Tenant HTTP 400s on any limit above 20 (verified live: 20 -> 200, 21 -> 400).
_PAGE_SIZE = 20
_MAX_LIMIT = 20

# India WID under the nested "locationHierarchy1" facet (see module docstring)
# -- tenant-specific, not the shared cross-tenant India GUID used elsewhere.
_INDIA_WID = "2fcb99c455831013ea52b82135ba3266"

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS BrassRing / MUFG): requesting offset >= total does NOT return
# an empty jobPostings array -- it silently re-returns page 1 verbatim.
# Track each keyword's first-page first job ID and treat a repeat of it on a
# later page as "no more results" so matcher.py's `if not page: break`
# pagination loop actually terminates instead of re-fetching the same page
# until max_listings is exhausted.
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
    "Referer": f"{_BASE_URL}/NVIDIAExternalCareerSite",
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
        "appliedFacets": {"locationHierarchy1": [_INDIA_WID]},
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
                raise RateLimitError("Nvidia Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Nvidia fetch failed: {exc}") from exc

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
        # Already pre-filtered to India via the locationHierarchy1 facet --
        # make sure the literal substring is present for matcher.py's
        # is_india_job() check. Most single-site postings already say
        # "India, Bengaluru"; multi-site rollups show "2 Locations".
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

    Returns (description_text, posting_date). The startDate field in the
    detail response is already ISO format (YYYY-MM-DD), so no relative-date
    conversion is needed here.
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/NVIDIAExternalCareerSite/", 1)[-1]
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
                raise RateLimitError("Nvidia description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Nvidia description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
