"""Fetches Intel Corporation job listings via the Workday public REST API.

ATS identification (Step 1, verified live 2026-09-13, not guessed): Intel's
branded careers front door (jobs.intel.com) 403s at the edge (a corporate
redirector WAF), but the real ATS was found by probing the Workday tenant
naming convention directly rather than trusting any secondary-research
label: intel.wd1.myworkdayjobs.com/External returns HTTP 200 and
POST /wday/cxs/intel/External/jobs returns real jobPostings (590 global
jobs at verification time) — a standard Workday CXS tenant, same REST
pattern as Nvidia/Micron/dozens of others already in this repo.

Like Genpact, this tenant exposes no usable flat country facet
(``locationCountry``/``Location_Country``/``Country_and_Jurisdiction`` all
absent) — the only location facet is ``locationMainGroup``, itself nesting
an inner ``locations`` facet with individual site-level entries. India's
sole entry is "India, Bangalore" (WID
``1e4a4eb3adf101f44070f976bf8184cf``, tenant-specific — not the shared
cross-tenant India GUID used at Fidelity/Citi/Wells Fargo/etc.), 52 jobs at
verification time. Applying it via ``appliedFacets: {"locations": [...]}``
narrows correctly (confirmed: every returned ``locationsText`` genuinely
names Bangalore/India, no leakage).

Same "HTTP 400 above limit 20" quirk as Nvidia/Northern Trust/MUFG-family
tenants (tested: 20 -> 200 OK, 60 -> 400) — clamped to 20 here. Unlike
Nvidia, standard offset-based pagination (0/20/40) does NOT wrap around on
this small a pool — it just runs out cleanly (confirmed: offset=20/40 return
distinct non-overlapping postings, offset=52+ returns an empty list, no
repeat-of-page-1 behavior observed at this pool size). Keyword search
(``searchText``) is genuinely server-side (confirmed: empty vs.
"software engineer" vs. a nonsense string all return different totals) —
not added to ``_IGNORES_KEYWORDS``.

Intel's Bangalore GCC pool (52 jobs) is, as expected for a chip-design
company, overwhelmingly ASIC/RTL/circuit-design/verification/physical-design
hardware roles — but a real, if thin, software-engineering track exists
alongside it: "Senior Development Tools Software Engineer" (Python/pytest/
CI-CD PDK tooling, mentions "AI tools"/"AI agents" generically but no hard
``primary_skills`` term today), "Software Application Development Engineer",
and "Enterprise Application Development Engineer". None of the three
currently name a hard ``.NET/C#`` or ``AI/ML/Python`` primary-skill term in
their JD body, so 0 current matches is expected — same
"confirmed-low-volume-is-real" class already documented for Micron/TI/
Genpact in this repo, not a fetcher defect. The pipeline is wired correctly
and will alert the moment a qualifying posting appears.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://intel.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/intel/External/jobs"
_JOB_BASE = f"{_BASE_URL}/External"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/intel/External"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# India, Bangalore WID under the nested "locations" facet (see docstring) --
# tenant-specific, not the shared cross-tenant India GUID used elsewhere.
_INDIA_WID = "1e4a4eb3adf101f44070f976bf8184cf"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/External",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


def _parse_posted_on(posted_on: str) -> str:
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
        "appliedFacets": {"locations": [_INDIA_WID]},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(_SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Intel Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Intel fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

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


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date). startDate is already ISO
    (YYYY-MM-DD), no relative-date conversion needed.
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/External/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                raise RateLimitError("Intel description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Intel description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
