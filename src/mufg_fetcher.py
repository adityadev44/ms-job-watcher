"""Fetches MUFG (Mitsubishi UFJ Financial Group) job listings via the Workday
public REST API.

MUFG's careers page (careers.mufgamericas.com and mufg.com/careers) redirects
to its Workday tenant at mufgub.wd3.myworkdayjobs.com (tenant "mufgub", site
"MUFG-Careers"). Confirmed live 2026-08-30:
POST to /wday/cxs/mufgub/MUFG-Careers/jobs returns real jobPostings.

India is filtered server-side via a "Country" facet (NOT "locationCountry"
like Fidelity/Wells Fargo, NOT "Country_and_Jurisdiction" like Citi — this
tenant uses the plain "Country" key). India WID
c4f78be1a8f14da0ab49ce1162348a5e is the same cross-tenant India GUID used by
Fidelity/Wells Fargo/Citi/Northern Trust/Accenture. ~214 total India jobs.

Quirk (same bug class as Accenture/Northern Trust — see PLAYBOOK): this
tenant HTTP 400s on any `limit` above 20 (tested: 20 -> 200 OK, 21 -> 400,
25/30/40 -> 400). fetch_jobs clamps `num` to 20 defensively. Separately, the
`total` field in the response resets to 0 on every paginated request
(offset > 0) even though `jobPostings` still contains a full page of real
results — identical to the documented Accenture bug. Never use `total` as a
pagination signal; only an empty `jobPostings` array means "no more pages"
(this is already how matcher.py's generic pagination loop works, since it
keys off `if not page: break`, not any total count).

Most India postings' locationsText is a facility name with no "India"
substring (e.g. "MUFG Global Service Private Ltd. - Bengaluru (BCIT)"); a
minority say "India - Bengaluru Branch" directly. Since the Country facet
already guarantees every result is genuinely India, ", India" is appended
client-side when the substring is absent — same fix as Citi/Barclays/Maersk.

Many titles are generic BPO/back-office bands (Analyst, AML, KYC, Internal
Audit, Business Analyst) that the shared title_family filter already screens
out; the few that do pass (Full Stack Developer, .NET Developer, Data
Engineer, etc.) are still a mix of legit dev roles and ops/support roles
that happen to use "Engineer"/"Developer" in the title band without being
software-engineering roles — verified live before deciding on
`require_tech_in_description` (see registry entry rationale).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://mufgub.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/mufgub/MUFG-Careers/jobs"
_JOB_BASE = f"{_BASE_URL}/MUFG-Careers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/mufgub/MUFG-Careers"

# Tenant HTTP 400s on any limit above 20 (verified live: 20 -> 200, 21 -> 400).
_PAGE_SIZE = 20
_MAX_LIMIT = 20

# India country WID -- the standard Workday global India reference ID, same
# GUID as Fidelity/Wells Fargo/Citi/Northern Trust/Accenture. Exposed under
# the plain "Country" facet key on this tenant (discovered from an
# unfiltered search's own facets list).
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS BrassRing): requesting offset >= total does NOT return an
# empty jobPostings array -- it silently re-returns page 1 verbatim, with
# `total` field back to its correct (non-zero) value, forever. Verified live
# for ".NET developer" (total=36): offset 36/40/56 all replayed the exact
# same 20 job IDs as offset 0. Track each keyword's first-page first job ID
# and treat a repeat of it on a later page as "no more results" so
# matcher.py's `if not page: break` pagination loop actually terminates
# instead of re-fetching the same page until max_listings is exhausted.
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
    "Referer": f"{_BASE_URL}/MUFG-Careers",
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
        "appliedFacets": {"Country": [_INDIA_WID]},
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
                raise RateLimitError("MUFG Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"MUFG fetch failed: {exc}") from exc

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
        # Already pre-filtered to India via the Country facet -- make sure the
        # literal substring is present for matcher.py's is_india_job() check.
        # Most facility names ("MUFG Global Service Private Ltd. - Bengaluru
        # (BCIT)") carry no country text at all; multi-site rollups show
        # "2 Locations".
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
        ext_path = "/" + application_url.split("/MUFG-Careers/", 1)[-1]
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
                raise RateLimitError("MUFG description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"MUFG description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
