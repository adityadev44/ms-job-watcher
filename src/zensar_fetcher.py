"""
Zensar Technologies job fetcher — Oracle HCM Cloud Candidate Experience REST API.

Tenant: fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com  |  Site: CX_1
Same REST pattern already used in this repo for Hexaware/Chubb/Amex/JPMorgan/
Icertis/DTCC/BNY/Dell/WTW — plain HTTP requests, no Playwright needed.

Zensar's own marketing careers page (`www.zensar.com/careers`) is a Next.js
front-end that reveals nothing about an ATS in its rendered HTML (jobs load
client-side), but its own `Content-Security-Policy` response header lists
`https://fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com` under `connect-src` —
that's the real backing tenant, confirmed live (2026-09-06) by loading
`https://fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/
en/sites/CX_1/jobs` directly: its `<base ... data-sitenumber="CX_1">` matches
and the page is a live, populated Oracle Fusion Recruiting Cloud "Candidate
Experience" job board.

A second, decoy-looking lead — `zensar.submit4jobs.com` (an old ColdFusion-
cookied legacy careers portal, still resolving with HTTP 200) — was checked
and ruled out: its HTML is a stale cached Drupal 7 snapshot of an unrelated
old zensar.com page (`<meta name="generator" content="Drupal 7">`, canonical
URL `.../about-us/contact-us`, body class `node-type-webform`), not a real,
current job listing. A new instance of this repo's standing "branded/legacy
domain isn't proof of anything" lesson — this one abandoned rather than
gated.

Verified via direct requests against the live REST API (2026-09-06):
- `keyword` genuinely narrows results server-side: a nonsense token and the
  literal phrase `"dot net"` both return `TotalJobsCount: 0`, while real
  terms return real, different counts (".NET developer": 18, "python
  developer": 40, "software development engineer": 128, "engineer": 157) —
  NOT registered in `_IGNORES_KEYWORDS`.
- **New gotcha, distinct from every other Oracle-CX tenant in this repo: a
  literal empty-string keyword (`keyword=""`) returns `TotalJobsCount: 0`**
  on this tenant, not the full unfiltered pool (Hexaware/Chubb-style tenants
  return everything for a blank keyword). Confirmed harmless for this
  fetcher's real contract — matcher.py's `keywords` list from config.yaml is
  never blank in production — but worth remembering if anyone is tempted to
  probe this tenant with an empty query expecting a full-pool count later.
- A real India location facet exists and is reliable server-side:
  `selectedLocationsFacet=300000000435151` (the `"India"` node the API's own
  `locationsFacet` list returns) — verified across 3 keywords with zero
  non-India leakage in any result page, and its own `TotalJobsCount` matches
  the unfiltered `locationsFacet` count for `"India"` exactly. Used here
  (same pattern as `chubb_fetcher.py`) rather than the broader "fetch
  globally, filter by 'india' client-side" fallback (Hexaware/Icertis/WTW),
  since a dedicated facet is available and confirmed accurate.
- Pagination is clean: `limit=100` and `offset=100` on the same 128-result
  query returned two disjoint pages (zero ID overlap) with the total held
  constant across both — no wraparound, no silent no-op.
- No joint/ambiguous multi-city postings observed in ~150 sampled India
  results (`secondaryLocations` is empty on every sample; `PrimaryLocation`
  is always a single clean `"City, State, India"` or bare `"India"` string)
  — none of the SimCorp-style "N Locations" or Eurofins-style joint-city
  handling this repo has needed elsewhere was needed here.
- One data-quality artifact, not a fetcher bug: a handful of titles carry a
  mis-encoded em-dash rendered as literal replacement-character garbage
  (e.g. "Senior Quality Engineer / QE Lead ��� AI-Led Quality
  Engineering") even when the raw bytes are read as UTF-8 directly — a
  server-side double-encoding artifact in Zensar's own tenant data, not
  something this fetcher can or should clean up (the term itself doesn't
  affect any matcher check either way).

Job descriptions are NOT inline in the search response — fetched from the
same Oracle REST detail endpoint (`recruitingCEJobRequisitionDetails`),
concatenating `ExternalDescriptionStr` + `ExternalResponsibilitiesStr` +
`ExternalQualificationsStr` (same three-field convention as every other
Oracle-CX fetcher here).
"""
from __future__ import annotations

import re
import time

import requests

_BASE_URL = "https://fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com"
_SEARCH_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_URL = f"{_BASE_URL}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_BASE = f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX_1/job"

_SITE_NUMBER = "CX_1"
# The "India" node from this tenant's own locationsFacet list — confirmed
# live (2026-09-06) to filter server-side with zero non-India leakage. See
# module docstring.
_INDIA_LOCATION_FACET_ID = 300000000435151
_PAGE_SIZE = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "ora-irc-language": "US",
    "Referer": f"{_BASE_URL}/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Oracle HCM Cloud."""


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
) -> list[dict]:
    """Return a page of Zensar's India job postings.

    India is scoped server-side via `selectedLocationsFacet` (verified
    reliable — see module docstring); `keyword` genuinely narrows results
    server-side too. `location` (the config-level facility param) is not
    used — matches every other single-country-facet fetcher in this repo.
    """
    finder = (
        f"findReqs;siteNumber={_SITE_NUMBER},"
        f"facetsList=LOCATIONS,"
        f"limit={num},"
        f"offset={start},"
        f'keyword="{keyword}",'
        f"sortBy=RELEVANCY,"
        f"selectedLocationsFacet={_INDIA_LOCATION_FACET_ID}"
    )
    params = {
        "onlyData": "true",
        "expand": "requisitionList.workLocation,requisitionList.secondaryLocations",
        "finder": finder,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Zensar: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zensar search failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Zensar fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Zensar search returned non-JSON body: {exc}") from exc

    items = data.get("items", [])
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
        posted_date = (j.get("PostedDate") or "")[:10]
        jobs.append({
            "id": str(job_id),
            "title": (j.get("Title") or "").strip(),
            "location": location_str,
            "posting_date": posted_date,
            "application_url": f"{_JOB_BASE}/{job_id}",
        })
    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date via the Oracle HCM detail API."""
    job_id = application_url.rstrip("/").split("/")[-1]

    finder = f'ById;Id="{job_id}",siteNumber={_SITE_NUMBER}'
    params = {
        "expand": "all",
        "onlyData": "true",
        "finder": finder,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_DETAIL_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Zensar description: 429 rate-limited for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zensar description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Zensar description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        return "", ""

    items = data.get("items", [])
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
