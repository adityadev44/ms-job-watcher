"""Fetches Caterpillar Inc. job listings via the Workday public REST API.

ATS discovery (live, 2026-09-14): ``careers.caterpillar.com`` is a Phenom-
style Umbraco-CMS-driven career-site *frontend*, but it is Cloudflare-
managed-challenge-gated (plain ``requests``/``curl`` get a clean 403
``cf-mitigated: challenge``; headless Firefox passes the challenge
transparently, same as Cummins). Rather than run every listing/detail
fetch through Playwright, the real backend was found by following a real
job's own "Apply" link (per the playbook's own "trace the apply
destination" lesson): it resolves to
``cat.wd5.myworkdayjobs.com/en-US/CaterpillarCareers/job/.../apply`` --
this frontend is a Workday tenant skin, exactly like the Boeing/GE
Aerospace "one ATS backend behind another vendor's frontend" pattern. The
Workday CXS API itself (tenant ``cat``, site ``CaterpillarCareers``, host
``wd5``) accepts plain unauthenticated POST requests with zero Cloudflare
gating -- Playwright is never needed anywhere in this pipeline.

India is filtered via a *nested* facet -- unlike Wells Fargo/Accenture/3M/
Caterpillar-adjacent tenants that expose a flat ``locationCountry``
facet, this tenant's only top-level location facet is
``locationMainGroup``, whose own values list is a single
``locationCountry`` sub-facet. India's WID is the same cross-tenant GUID
used elsewhere in this repo: ``c4f78be1a8f14da0ab49ce1162348a5e``
(confirmed live: 69 India jobs at investigation time).

Two Workday quirks confirmed live and handled here:
1. **Accenture-style ``total: 0`` on offset > 0** -- ``total`` in the JSON
   response is only reliable on the first page; later pages report 0 even
   though ``jobPostings`` still has real, correct data. Termination uses
   an empty ``jobPostings`` list, not ``total``.
2. **UBS/MUFG/Nvidia-style pagination wraparound** -- past a certain
   offset this tenant starts returning the *same* first page of results
   over and over rather than an empty list or a clean error. Guarded by
   tracking the first job ID seen on each page and stopping once a page's
   first ID repeats a previously-seen page's first ID.

Real non-Chennai/non-Pune matches confirmed live in Bangalore, Karnataka
(not an excluded city): "Software Engineer- .Net Developer with Azure",
"Senior Software Engineer- Dynamics 365 (D365) F&O", "Senior Java
Developer", "Software Engineer - D365 CE", "Principal Software Engineer
Distributed Systems & Cloud-Native Platform (Java)" -- a genuine .NET/C#
signal exists here, not just AI/ML-adjacent titles.

Locations are mostly clean city/state text ("Bangalore, Karnataka",
"Chennai, Tamil Nadu") that already lets matcher.py's exclude_locations
catch Chennai/Tamil Nadu correctly, but never contain the literal
substring "india" -- ", India" is appended. Multi-site postings show as
bare "2 Locations"/"3 Locations" (same ambiguous-location shape as
Maersk/SimCorp/3M elsewhere in this repo); treated as bare "India" since
the whole pool is already pre-filtered by the India WID.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://cat.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/cat/CaterpillarCareers/jobs"
_JOB_BASE = f"{_BASE_URL}/CaterpillarCareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/cat/CaterpillarCareers"
_PAGE_SIZE = 20

# India locationCountry WID -- same cross-tenant GUID as Wells Fargo/Citi/3M.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/CaterpillarCareers",
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


def _normalize_location(loc: str) -> str:
    loc = (loc or "").strip()
    if not loc:
        return "India"
    if "india" not in loc.lower():
        loc = f"{loc}, India"
    return loc


def _request(method: str, url: str, *, json_body: dict | None = None, timeout: int = 20):
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if method == "POST":
                    r = requests.post(url, headers=_HEADERS, json=json_body, timeout=timeout, verify=False)
                else:
                    r = requests.get(url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Caterpillar Workday: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Caterpillar fetch failed: {exc}") from exc
    raise RateLimitError("Caterpillar fetch failed: exhausted retries")


# Module-level cache of each keyword's page-0 first job ID -- used to detect
# the wraparound-past-total bug (a later offset for the SAME keyword
# silently repeating page 0) without needing a full-pool cache-once fetcher.
# Keyed by keyword so two different searches' legitimately-shared first job
# can't cause a false-positive stop.
_page0_first_id: dict[str, str] = {}


def _job_id_from(p: dict) -> str:
    external_path = p.get("externalPath", "")
    for field in p.get("bulletFields", []):
        m = re.match(r"^(R\d+)$", field.strip(), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    m = re.search(r"_(R\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
    if m:
        return m.group(1).upper()
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
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }
    r = _request("POST", _SEARCH_URL, json_body=body, timeout=timeout)
    postings = r.json().get("jobPostings", [])
    if not postings:
        return []

    first_id = _job_id_from(postings[0])
    if start == 0:
        if first_id:
            _page0_first_id[keyword] = first_id
    elif first_id and _page0_first_id.get(keyword) == first_id:
        # Wraparound: this offset repeated this keyword's own page-0 first job.
        return []

    jobs: list[dict] = []
    for p in postings:
        job_id = _job_id_from(p)
        title = p.get("title", "").strip()
        if not job_id or not title:
            continue

        external_path = p.get("externalPath", "")
        loc = p.get("locationsText", "").strip()
        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": _normalize_location(loc),
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/CaterpillarCareers/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = _request("GET", api_url, timeout=timeout)
    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""
    return description, posting_date
