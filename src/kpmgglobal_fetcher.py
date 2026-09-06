"""Fetches KPMG Global Services job listings via Oracle HCM Cloud
Candidate Experience (CE) REST API.

kpmg.com/in/en/careers.html links out to two different systems: a generic
"experienced professionals" board at kpmgindia.talentrecruit.com (an Angular
SPA on a new-to-this-repo ATS vendor, "TalentRecruit" -- blocked from direct
DevTools inspection in this environment and not pursued further since it
is not the GCC entity this task targets), and the actual "KPMG Global
Services Careers" site at `ejgk.fa.em2.oraclecloud.com` (confirmed via web
search, matching the `ejgk.fa.em2.oraclecloud.com` signature already visible
in kpmg.com/in/en/careers.html's own page source) -- KPMG's ~24,000-person
GCC/GDC organization (Bengaluru, Gurugram, Hyderabad, Mumbai, Kochi, Noida,
Kolkata, Pune). `/careers` 302-redirects to
`/hcmUI/CandidateExperience/en/sites/CX_3`, giving the site number (`CX_3`)
this fetcher needs. Plain `requests` works with no session/cookie bootstrap
and no Playwright -- same REST pattern as chubb_fetcher.py/bny_fetcher.py.

Unlike most Oracle HCM CE tenants in this repo, this tenant's ENTIRE pool is
already India-only -- there is no separate global pool to filter down from.
Confirmed live: an unfiltered `findReqs` query (no keyword) returns
`TotalJobsCount: 699`, and the same query's own `locationsFacet` shows the
top-level "India" facet at `TotalCount: 699` -- an exact match, meaning every
job on this board is already India-based. No location facet is applied in
the query for this reason (there is nothing to scope down); a defensive
`"india" not in location.lower()` client-side check is still kept in case
that ever changes.

Passing a literal empty keyword (`keyword=""`) in the finder string is NOT
"no filter" on this tenant -- it makes the API filter on an exact-match
empty string and return `TotalJobsCount: 0` (confirmed by testing with and
without the keyword clause present at all). Since `fetch_jobs` here is only
ever called with real keyword strings from config (never blank), this is
noted but never actually hit in production use.

`keyword` genuinely narrows server-side, though NOT as simple per-word AND
matching would predict -- e.g. "software engineer" (2 words) returns 51,
but "senior software engineer" (3 words) returns 109, larger despite adding
a word (a nonsense-token combination like "zzzz engineer" still correctly
returns 0, ruling out a pure noisy OR-match across whole terms). This looks
like Oracle's search relevance engine falling back to a looser
minimum-should-match combiner for longer phrases rather than strict phrase
AND. Not fully characterized, but every one of the ten default keywords
still returns well under the ~699-job total (worst case 109), so `kpmgglobal`
is NOT registered in `_IGNORES_KEYWORDS` -- per-keyword querying still adds
real precision and keeps each pass's page count small. One quirk worth
flagging: the literal two-word keyword "dot net" returns 0 matches (real
.NET postings apparently spell it ".NET"/"DotNet", never "dot net" as two
separate words) while ".NET developer" returns 12 -- no recall is actually
lost since both terms are independently in the shared default keyword list
and matcher.py dedupes across keyword passes, but it means the "dot net"
pass is always empty for this company specifically.

**A required, easy-to-miss quirk**: without an explicit `expand=
requisitionList.workLocation,requisitionList.secondaryLocations` query param,
this tenant's search API still computes `TotalJobsCount` correctly but
silently returns an EMPTY `requisitionList` array for every `sortBy` value
tried (RELEVANCY, POSTING_DATES_DESC, POSTING_DATES, TITLES) -- confirmed
live, not a rate-limit or bot-block symptom. Chubb's near-identical fetcher
in this repo already sends this expand for a different reason (resolving
`workLocation`/`secondaryLocations`); here it turns out to be load-bearing
for getting any results back at all.

`PostedDate` is already `YYYY-MM-DD` and `PrimaryLocation` already contains
the literal word "India" (e.g. "Bangalore, Karnataka, India") -- no date
parsing or location-string patching needed, unlike every SuccessFactors J2W
fetcher in this same onboarding batch.
"""

from __future__ import annotations

import re
import time

import requests

_BASE_URL = "https://ejgk.fa.em2.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX_3/job"

_SITE_NUMBER = "CX_3"
_PAGE_SIZE = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "ora-irc-language": "US",
    "Referer": f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/{_SITE_NUMBER}/jobs",
}

_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Oracle HCM CE."""


def _strip_html(raw: str) -> str:
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    global _FIRST_PAGE_IDS

    safe_keyword = keyword.replace('"', "")
    finder = (
        f"findReqs;siteNumber={_SITE_NUMBER},"
        f"facetsList=LOCATIONS,"
        f"limit={num},"
        f"offset={start},"
        f'keyword="{safe_keyword}",'
        f"sortBy=POSTING_DATES_DESC"
    )
    params = {
        "onlyData": "true",
        "finder": finder,
        # Required: without an explicit expand, this tenant's API computes
        # TotalJobsCount correctly but silently returns an EMPTY
        # requisitionList (confirmed live for every sortBy value tried) --
        # not a rate-limit or bot-block symptom, just an omitted expand.
        "expand": "requisitionList.workLocation,requisitionList.secondaryLocations",
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("KPMG Global Services: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"KPMG Global Services fetch failed: {exc}") from exc

    try:
        items = r.json().get("items", [])
    except ValueError as exc:
        raise RateLimitError(f"KPMG Global Services returned non-JSON body: {exc}") from exc
    if not items:
        return []

    req_list = items[0].get("requisitionList", []) or []

    jobs: list[dict] = []
    for j in req_list:
        job_id = j.get("Id", "")
        if not job_id:
            continue
        location_str = j.get("PrimaryLocation", "") or ""
        if "india" not in location_str.lower():
            continue
        title = (j.get("Title") or "").strip()
        if not title:
            continue
        posted_date = (j.get("PostedDate") or "")[:10]
        jobs.append({
            "id": str(job_id),
            "title": title,
            "location": location_str,
            "posting_date": posted_date,
            "application_url": f"{_JOB_BASE}/{job_id}",
        })

    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and jobs and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []  # wraparound detected

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    job_id = application_url.rstrip("/").split("/")[-1]

    finder = f'ById;Id="{job_id}",siteNumber={_SITE_NUMBER}'
    params = {"expand": "all", "onlyData": "true", "finder": finder}

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_DETAIL_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"KPMG Global Services description: 429 for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    try:
        items = r.json().get("items", [])
    except ValueError:
        return "", ""
    if not items:
        return "", ""

    job = items[0]
    desc_parts = [
        job.get("ExternalDescriptionStr") or "",
        job.get("ExternalResponsibilitiesStr") or "",
        job.get("ExternalQualificationsStr") or "",
    ]
    combined_html = " ".join(p for p in desc_parts if p)
    description = _strip_html(combined_html)

    raw_date = job.get("ExternalPostedStartDate") or ""
    posting_date = raw_date[:10] if raw_date else ""

    return description, posting_date
