"""Fetches insightsoftware job listings via the Workday public REST API.

insightsoftware's ATS is Workday, but NOT hosted under a
"insightsoftware*"-named tenant -- it runs on the tenant left over from its
2021 acquisition of Magnitude Software: `magnitudesoftware.wd1.myworkdayjobs
.com`, site "External". Confirmed live 2026-09-04: the public
`insightsoftware.com/careers/` marketing page's own "View Job" links point
straight at this tenant, and the CXS search endpoint returns real,
current insightsoftware postings (hiringOrganization.name ==
"insightsoftware International Pvt. Ltd." on the India job details) --
this is not a stale/orphaned Magnitude board, it's insightsoftware's real,
active careers portal. Same "company name != tenant name" lesson as
Macquarie/Macquerie-University and Barclays/Boeing's skinned-frontend cases,
just for an acquired-company tenant instead of an unrelated
same-named org or a frontend skin.

Plain REST, no browser required -- same CXS shape as Thomson
Reuters/Genpact/Fiserv.

**No usable location facet.** The only facets this tenant exposes are
jobFamilyGroup/workerSubType/timeType -- no locationCountry-style facet at
all (same shape as Genpact/Fiserv/FactSet). Jobs are fetched globally per
keyword (searchText genuinely narrows server-side -- confirmed: "Software
Engineer" returns 45 of the ~99 total global postings) and filtered for
India client-side via `locationsText`.

**`total` resets to 0 on every page after the first** (same silent-reset
quirk as Accenture/Genpact's `total` field) -- pagination termination relies
on `len(page) < limit`, not `total`, same as every other fetcher already
built against this quirk.

**Max page size is 20** -- `limit=99` (or any value >20) returns a bare
HTTP 400, same "silent cap" family as Northern Trust/Thomson Reuters.

**New wrinkle not previously seen in this repo: ~5% of postings carry NO
`locationsText` field at all in the search response** (not "2 Locations",
not a blank string -- the key is simply absent), even though the job is a
genuine single-location India posting. Confirmed via a live example:
"Senior Software Engineer (C#, .NET, SQL)" (REQ000141) has no
`locationsText` in the list response, but its detail page's
`jobRequisitionLocation.descriptor` says "India - Hyderabad" and its JD
contains real C#/SQL Server content -- naively skipping every job with a
missing `locationsText` field (as Genpact/Fiserv's `"india" not in loc`
short-circuit would do, since `""` never contains "india") would have
silently dropped a real, on-target match. Fixed here by falling back to a
one-off detail-page lookup (`jobRequisitionLocation.descriptor`, then the
plain `location` field) whenever `locationsText` is missing, with a small
in-module cache keyed by `externalPath` so `fetch_job_description()` never
re-fetches the same detail page a second time for jobs already resolved
this way.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://magnitudesoftware.wd1.myworkdayjobs.com"
_TENANT = "magnitudesoftware"
_SITE = "External"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"

# Workday rejects limit > 20 for this tenant with a bare HTTP 400.
_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_JOB_BASE}",
}

# externalPath -> raw jobPostingInfo dict, so a job resolved via a
# location-lookup detail fetch in fetch_jobs() doesn't get re-fetched again
# by fetch_job_description() moments later.
_DETAIL_CACHE: dict[str, dict] = {}


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


def _get_detail(external_path: str, timeout: int) -> dict:
    """GET the CXS detail API for one job, cached by externalPath.

    Returns the raw ``jobPostingInfo`` dict, or ``{}`` on any failure --
    callers treat that as "location/description unknown", not a hard error,
    since this is used opportunistically (location backfill) as well as for
    the required ``fetch_job_description`` contract.
    """
    if external_path in _DETAIL_CACHE:
        return _DETAIL_CACHE[external_path]

    api_url = f"{_DETAIL_BASE}{external_path}"
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("insightsoftware detail: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {}

    if r is None:
        return {}
    info = r.json().get("jobPostingInfo", {}) or {}
    _DETAIL_CACHE[external_path] = info
    return info


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
        "limit": min(num, _PAGE_SIZE),
        "offset": start,
        "searchText": keyword,
    }

    r = None
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
                raise RateLimitError("insightsoftware Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"insightsoftware fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError("insightsoftware fetch: no response")

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")
        if not external_path:
            continue

        title = (p.get("title") or "").strip()
        if not title:
            continue

        loc = (p.get("locationsText") or "").strip()
        if not loc:
            # ~5% of postings omit locationsText entirely even though they
            # are genuine single-location jobs -- see module docstring.
            # Resolve via a one-off detail lookup rather than silently
            # dropping (or silently guessing) the location.
            detail = _get_detail(external_path, timeout)
            req_loc = (detail.get("jobRequisitionLocation") or {}).get("descriptor", "")
            loc = (req_loc or detail.get("location") or "").strip()
        if not loc:
            continue

        job_id = ""
        for field in p.get("bulletFields", []) or []:
            m = re.match(r"^(REQ\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(REQ\d+)", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": f"{_JOB_BASE}{external_path}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]

    info = _get_detail(ext_path, timeout)
    if not info:
        raise RateLimitError(f"insightsoftware description fetch failed for {application_url!r}")

    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
