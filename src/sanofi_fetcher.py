"""Fetches Sanofi job listings via the Workday public REST API.

Sanofi's ATS is Workday, hosted at sanofi.wd3.myworkdayjobs.com (tenant
"sanofi", site "SanofiCareers" -- confirmed live 2026-09-13 via a direct
POST to /wday/cxs/sanofi/SanofiCareers/jobs, which returned real
jobPostings (HTTP 200, total=831 globally). Found directly from a live
web search turning up the real
`sanofi.wd3.myworkdayjobs.com/SanofiCareers` board URL.

This tenant's facet response nests location facets one level deep with a
usable `locationCountry` facet, the same shape as Novartis/Wells Fargo/
Fidelity/Pfizer -- and the India WID (`c4f78be1a8f14da0ab49ce1162348a5e`)
is the same cross-tenant GUID reused across those tenants plus Citi/
Northern Trust/MUFG/Target/Shell. Passing
`{"locationCountry": [india_wid]}` in `appliedFacets` narrows the global
831-job pool to a genuine India-only 72-job pool (confirmed: every
returned `locationsText` is a bare Indian city -- Hyderabad, Mumbai,
Bangalore, Verna [Goa] -- no leakage).

Confirmed live 2026-09-13: Sanofi's current India pool skews heavily
non-engineering -- scientific writing, data/business analysts, regulatory
affairs, HR, medical affairs. A `searchText="software engineer"` pass
returns only 1 result ("Principal Stats Programmer"), and
`searchText="generative ai"` returns 0. This is a real, current fact about
this specific snapshot (same disposition as the documented Novartis/
Airbnb/DoorDash "0 or near-0 matches is a true reflection of the current
posting pool, not a fetcher defect" precedent) -- Sanofi's India
GCC/Hub hiring today is dominated by commercial/scientific/analytics
roles rather than software/platform engineering, unlike J&J and Roche's
same-wave results. Keyword search genuinely narrows server-side (72 -> 1
for "software engineer"), so this fetcher is not added to
`_IGNORES_KEYWORDS`.

`locationsText` on this tenant is always a bare city name (e.g.
"Hyderabad", "Mumbai", "Verna") -- never containing the literal "India"
substring. ", India" is appended client-side when missing, safe because
results are already pre-filtered by the India country facet server-side
(same pattern as Novartis/Fidelity/Pfizer).

Two Workday quirks confirmed live on this tenant, the second an ACTIVE bug
(unlike J&J/Roche, where the same defensive guard was cheap insurance
against a bug that didn't reproduce in testing):
- Page size is capped at 20 -- limit=25 returns a raw HTTP 400 (same class
  as Northern Trust/Pfizer/Novartis's cap).
- Pagination wraps around past the true total (same bug class as UBS/
  MUFG/Nvidia/Pfizer/Walmart/Novartis): confirmed live with the India
  facet applied (total=72) -- offset=60 correctly returned the real final
  12 jobs with `total` reverting to 0 (the Accenture/Novartis "total
  reverts to 0 on a real last page" quirk, also present here), but
  offset=80 (past the true last page) silently replayed page 1's exact
  first job ID verbatim instead of returning an empty array. Guarded via
  the same page1-first-ID memo pattern as Novartis/Pfizer/MUFG/Nvidia/
  Walmart -- this guard is load-bearing here, not just defensive.

Job detail descriptions come from the same Workday CXS JSON detail API
shape as Novartis/J&J/Roche/Pfizer: GET
.../wday/cxs/sanofi/SanofiCareers{externalPath} returns a
`jobPostingInfo.jobDescription` HTML blob and an already-ISO
`jobPostingInfo.startDate` (confirmed live: "2026-09-02").
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://sanofi.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/sanofi/SanofiCareers/jobs"
_JOB_BASE = f"{_BASE_URL}/SanofiCareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/sanofi/SanofiCareers"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# Cross-tenant India country WID, shared with Novartis/Wells Fargo/Fidelity/
# Pfizer/Citi/Northern Trust/MUFG/Target/Shell.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS BrassRing / MUFG / Nvidia / Pfizer / Walmart / Novartis):
# requesting offset >= total does NOT return an empty jobPostings list --
# confirmed live it silently re-returns page 1 verbatim once past the
# genuine last page. Track each keyword's first-page first job ID and treat
# a repeat of it on a later page as "no more results".
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
    "Referer": f"{_BASE_URL}/SanofiCareers",
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
                raise RateLimitError("Sanofi Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Sanofi fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    # Detect the pagination wrap-around bug (see _page1_first_id comment
    # above) before doing any other work on this page -- confirmed live and
    # load-bearing on this tenant, unlike J&J/Roche's defensive-only guard.
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
        # Already pre-filtered to India via the country facet -- bare city
        # names ("Hyderabad", "Mumbai", "Verna") never say "India" on their
        # own here.
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
        ext_path = "/" + application_url.split("/SanofiCareers/", 1)[-1]
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
                raise RateLimitError("Sanofi description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Sanofi description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
