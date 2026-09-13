"""Fetches MKS Instruments job listings via the Workday public REST API.

ATS identification (Step 1, verified live 2026-09-13): MKS's branded
marketing career page (`mks.com/careers`) is an unrelated SAP Commerce/
Spartacus Angular shell that returns almost no server-rendered content and
is NOT the real job board. The real board is linked from a separate India-
specific landing page (`mks-india-careers.webflow.io`, itself referencing
"600+ professionals across five India office locations" spanning R&D
(Manesar), engineering (Bengaluru), and digital/IT (Gurugram)), whose
"View Open Roles" buttons point at a genuine Workday tenant:

    https://mksinst.wd1.myworkdayjobs.com/en-US/MKSCareersEMEA
        ?locationCountry=c4f78be1a8f14da0ab49ce1162348a5e

Tenant `mksinst`, site `MKSCareersEMEA` (an EMEA-named site that in practice
also covers India -- confirmed by the facet WID itself, the same
cross-tenant "India" GUID (`c4f78be1a8f14da0ab49ce1162348a5e`) already seen
on Wells Fargo/Citi-family tenants in this repo). Standard Workday CXS POST
API, no browser needed. Confirmed live: 25 India jobs with the
`locationCountry` facet applied, including genuine software-engineering
roles ("Senior Software Engineer I", "Technical Lead - AI/ML") alongside
mechanical/chemistry/IT-ops roles -- consistent with MKS's real photonics/
semiconductor-equipment engineering mix, not a coverage error.

Quirks found during live probing -- this tenant's pagination is unusually
unreliable, three distinct bugs stacked on top of each other:
- `limit` is hard-capped at 20 -- any value above that (21+) returns a bare
  HTTP 400 with no jobPostings body.
- For a keyword whose real result count is <= 20 (a single page), `offset`
  is ignored entirely -- `offset=0/20/40` with `searchText="software
  engineer"` (true total 8) all returned the identical 8-job first page,
  with `total: 8` reported correctly every time. Since `jobPostings` is
  never empty, a naive per-keyword pagination loop (stopping only on an
  empty page) would spin re-fetching the same page until `max_listings` --
  confirmed live: this is what actually happened, hanging matcher.py's
  fetch loop for several minutes on the very first narrow keyword tried.
- For the full unfiltered pool (`searchText=""`, true total 25, i.e. more
  than one page): `offset=0` correctly returns page 1 (20 jobs, `total:
  25`); `offset=20` correctly returns page 2 (5 new jobs) but with `total:
  0` in that same response (the Accenture-style "total resets on later
  pages" quirk); `offset=40` (past the true total) *wraps back around* and
  re-returns page 1's exact contents with `total: 25` again -- the same
  "pagination wraps past total" class already documented for UBS/MUFG/
  Nvidia/Pfizer/Walmart in PLAYBOOK.md.

None of `total`, `offset`, or "is jobPostings empty" is trustworthy alone
on this tenant. Rather than patch matcher.py's generic per-keyword
pagination with tenant-specific guards, this fetcher sidesteps the whole
problem: it caches the small (~25-job) India pool once per process with a
single unfiltered `searchText=""` sweep, walking pages until a page's
first job ID has already been seen (the reliable "wrapped around, stop"
signal for this tenant), and ignores the caller's keyword/location --
matcher.py's own client-side dedup already covers the narrowing that
server-side `searchText` would otherwise have provided. Same "small board,
cache once" discipline as Ferguson/AA Tech Hub India/Lufthansa/Boeing.

Other quirks:
- Every direct posting's `locationsText` already starts with the literal
  token "India" (e.g. "India Bangalore Whitefield", "India Gurgaon",
  "India Manesar") -- no Indiana/Indianapolis substring risk here (unlike
  Medtronic's tenant) since the format is a controlled facet string, not a
  free-text US city name.
- Some postings show ambiguous "2 Locations"/"3 Locations" instead of a
  named city (Workday's standard multi-site-req collapsing, same shape as
  Maersk/SimCorp elsewhere in this repo) -- since the `locationCountry`
  facet is already applied server-side, every returned posting is
  guaranteed to include an India site among its locations, so these are
  safely normalized to "India" rather than dropped or mis-tagged.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://mksinst.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/mksinst/MKSCareersEMEA/jobs"
_JOB_BASE = f"{_BASE_URL}/MKSCareersEMEA"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/mksinst/MKSCareersEMEA"
_PAGE_SIZE = 20  # Workday tenant hard-caps `limit` at 20; 21+ returns HTTP 400

# India locationCountry WID for MKS's Workday tenant -- same cross-tenant
# GUID used by Wells Fargo/Citi-family tenants. Verified 2026-09-13: returns
# 25 India results with an empty searchText.
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
    "Referer": f"{_BASE_URL}/en-US/MKSCareersEMEA",
}

_cache: list[dict[str, str]] = []
_cache_filled = False


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


def _post_with_retry(body: dict, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
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
                raise RateLimitError("MKS Workday: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"MKS fetch failed: {exc}") from exc
    raise RateLimitError(f"MKS fetch failed: {last_exc}")


def _parse_job(p: dict) -> dict[str, str] | None:
    external_path = p.get("externalPath", "")

    job_id = ""
    for field in p.get("bulletFields", []):
        m = re.match(r"^(R\d+)$", field.strip(), re.IGNORECASE)
        if m:
            job_id = m.group(1).upper()
            break
    if not job_id:
        m = re.search(r"_(R\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
        if m:
            job_id = m.group(1).upper()
    if not job_id:
        return None

    title = p.get("title", "").strip()
    if not title:
        return None

    loc = p.get("locationsText", "").strip()
    # Facet is already India-filtered server-side; ambiguous multi-site
    # postings ("2 Locations"/"3 Locations") are safely normalized to
    # "India" rather than dropped (same reasoning as Maersk's fetcher).
    if "india" not in loc.lower():
        loc = "India"

    app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

    return {
        "id": job_id,
        "title": title,
        "location": loc,
        "posting_date": _parse_posted_on(p.get("postedOn", "")),
        "application_url": app_url,
    }


def _fill_cache(timeout: int) -> None:
    """Walk the full India pool once with an unfiltered sweep.

    This tenant's `offset`/`total` semantics are unreliable at every edge
    (see module docstring) -- the only trustworthy termination signal found
    is a repeated job ID, which means the response wrapped back to a page
    already seen. Hard-capped at 20 pages (400 jobs) as defense-in-depth
    against an unexpected infinite loop.
    """
    seen_ids: set[str] = set()
    start = 0
    for _ in range(20):
        body = {
            "appliedFacets": {"locationCountry": [_INDIA_WID]},
            "limit": _PAGE_SIZE,
            "offset": start,
            "searchText": "",
        }
        r = _post_with_retry(body, timeout)
        postings = r.json().get("jobPostings", []) or []
        if not postings:
            break

        new_this_page = 0
        for p in postings:
            job = _parse_job(p)
            if job is None or job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            _cache.append(job)
            new_this_page += 1

        if new_this_page == 0:
            # Every job on this page was already cached -- the tenant
            # wrapped back around to an earlier page. Stop.
            break

        start += len(postings)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return MKS India job listings (cached once per process).

    Ignores `keyword`/`location` -- see module docstring for why this
    tenant's per-keyword pagination cannot be trusted. matcher.py's own
    client-side dedup across keyword/location combinations means this is
    safe: the full small pool is simply returned once and deduped there.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/MKSCareersEMEA/", 1)[-1]
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
                raise RateLimitError("MKS description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"MKS description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
