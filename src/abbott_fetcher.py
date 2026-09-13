"""Fetches Abbott Laboratories job listings via the Workday public REST API.

Abbott's careers site (www.abbott.com/en-us/careers) links two Workday
surfaces: a candidate-facing "Check application status" login at
abbott.wd5.myworkdayjobs.com/abbottcareers and the branded public search
front-end at www.jobs.abbott/us/en (a marketing/vanity domain that
proxies the same tenant). Confirmed live 2026-09-13 via a direct POST to
/wday/cxs/abbott/abbottcareers/jobs -- real data, HTTP 200, total=2000
(Workday's global result cap, same class as Accenture).

Standard flat `Location_Country` facet (capitalised key, same shape as
Marsh McLennan/State Street), India WID is the familiar cross-tenant GUID
reused across Fidelity/Wells Fargo/Citi/Northern Trust/MUFG/Target/
Pfizer/Shell/Novartis/MSD. Confirmed: total narrows from 2000 (global cap)
to 168 with the facet applied.

Confirmed live 2026-09-13: Abbott India has a genuine software/platform
engineering presence (Mumbai BKC + Hyderabad), not just pharma/medtech
R&D or field sales -- current postings include "Cloud Software Engineer
II", "DevOps Engineer – III", "Staff Platform Engineer" (Abbott's "Lingo"
biowearable division), "Cloud SW Verification Engineer III". None of
these 3 title-family-matching postings currently name a primary .NET/C#
or AI/ML/Python skill in their JD body (Staff Platform Engineer's stack is
Azure/Kubernetes/Terraform -- all broad-only terms; DevOps Engineer III
mentions bare "Python" without any of the primary AI/ML hard terms) -- so
0 current matches is a real, current fact about this specific snapshot,
not a fetcher defect, the same situation documented for Novartis. Do NOT
add `require_tech_in_description` -- titles are the limiting factor here
(most India postings are pharma/nutrition sales roles, e.g. "Territory
Business Manager", "Nutrition Sales Executive"), not an overly broad
skill list letting false positives through.

Abbott's own `searchText` param is a loose/noisy full-text OR-of-words
match against JD body content (same class as Novartis/SAP Labs/Yash) --
confirmed live: searchText=".net" surfaces unrelated sales-manager and
regional-coordinator postings, and searchText="generative ai" returns
115 of the 168 total India postings. Not narrowed here; the shared
matcher's title-family/skills gates do the real filtering downstream.

Two Workday quirks confirmed live on this tenant, both guarded here:
- Page size is capped at 20 -- limit=25/50 both return a raw HTTP 400
  (same class as Northern Trust/Pfizer/Novartis/MSD).
- Pagination wraps around past the true total (same bug class as
  UBS/MUFG/Nvidia/Pfizer/Walmart/Novartis): offset=180 (with only 168
  total) returns `total: 168` again and replays page 1's exact job set
  verbatim, rather than an empty list. Guarded via the same
  page1-first-ID memo pattern as those fetchers.

locationsText is reliably India-scoped ("India - Mumbai", "India -
Hyderabad", "INDIA - TAMIL NADU - CHENNAI", or occasionally a
"Country > Region : Site" format like "India > Village Mauza : Baddi")
and every observed India value already contains the literal substring
"India" -- no client-side append needed, unlike Novartis/MSD.

Job detail descriptions use the same Workday CXS JSON detail API shape as
Novartis/Pfizer/MSD: GET .../wday/cxs/abbott/abbottcareers{externalPath}
returns a `jobPostingInfo.jobDescription` HTML blob and an already-ISO
`jobPostingInfo.startDate`.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://abbott.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/abbott/abbottcareers/jobs"
_JOB_BASE = f"{_BASE_URL}/abbottcareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/abbott/abbottcareers"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# Standard cross-tenant Workday India country WID, reused across Fidelity/
# Wells Fargo/Citi/Northern Trust/MUFG/Target/Pfizer/Shell/Novartis/MSD.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS/MUFG/Nvidia/Pfizer/Walmart/Novartis): requesting offset >=
# total does NOT return an empty jobPostings array -- it silently re-returns
# page 1 verbatim once past the genuine last page. Track each keyword's
# first-page first job ID and treat a repeat of it on a later page as "no
# more results".
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
    "Referer": f"{_BASE_URL}/abbottcareers",
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
        "appliedFacets": {"Location_Country": [_INDIA_WID]},
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
                raise RateLimitError("Abbott Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Abbott fetch failed: {exc}") from exc

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
        ext_path = "/" + application_url.split("/abbottcareers/", 1)[-1]
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
                raise RateLimitError("Abbott description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Abbott description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
