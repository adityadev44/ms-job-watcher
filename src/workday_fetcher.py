"""
Workday (the company, as an employer) job fetcher — Workday's own public
CXS REST API, self-hosted on their own platform.

Careers site: https://www.workday.com/en-us/company/careers/overview.html
Workday tenant: workday.wd5.myworkdayjobs.com, site: Workday
Confirmed live (2026-09-06) via plain POST to the CXS endpoint — no
Playwright needed. Note this is Workday-the-employer's *own* corporate
careers page (their own Product Development/GTM/Advisory org hiring),
which — fittingly — also runs on their own Workday CXS platform, same
API shape already used in this repo as the ATS *vendor* for many other
companies (browserstack_fetcher.py, aon_fetcher.py, pwcac_fetcher.py,
etc. — all `<company>.wd#.myworkdayjobs.com`). This module is unrelated
to those; it is Workday hiring for itself.

Search endpoint: POST https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/jobs
  Body: {"appliedFacets": {...}, "limit": <n>, "offset": <start>, "searchText": "<keyword>"}
  - `searchText` is a genuine server-side filter (0 results for a nonsense
    token; 6/23 India results for "engineer"), so keywords genuinely
    narrow the result set.
  - Unlike BrowserStack's tenant (no usable location facet at all), this
    tenant exposes a real `Location_Country` facet with an "India" value
    (id `c4f78be1a8f14da0ab49ce1162348a5e`, confirmed live) — applied via
    `appliedFacets: {"Location_Country": ["c4f78be1a8f14da0ab49ce1162348a5e"]}`.
    Confirmed reliable: 23/375 global postings, zero non-India leakage
    observed in the sampled set.
  - Server hard-caps `limit` at 20 (requesting more returns HTTP 400,
    identical to the BrowserStack tenant) — all requests are defensively
    clamped to 20.
  - Pagination wraps around past the true total (standard Workday
    behavior for small tenants, same as BrowserStack) — terminated by the
    `_FIRST_PAGE_IDS` guard.

Location quirk: `locationsText` for this tenant is almost always
"IND.Pune" (a country-code-dot-city format, no literal "india" substring)
— only 2 of 23 current India postings use the human-readable "India,
Mumbai" form instead. Since the India facet itself already guarantees
every result here really is India (see above), `locationsText` is
normalised to "<City>, India" (stripping the "IND." prefix) rather than
trusted as-is, so both matcher.py's substring-based `is_india_job()` and
config's `exclude_locations` city checks see real text.

**Notable current fact, not a fetcher defect:** 21 of the 23 live India
postings are in Pune, which is on config's default `exclude_locations`
list — meaning almost this entire board is excluded by design under the
shared config, leaving effectively only the 2 Mumbai postings ("Principal
Functional Consultant - Workday Finance", "Senior Solution Consultant –
Workday Adaptive Planning") visible downstream, neither of which matches
the standard keyword list either. 0/23 match the standard keyword list
today regardless of the Pune exclusion — titles are Workday product
consultant/advisory-services roles ("Sr Prism Consultant", "Extend
Services Consultant") plus a handful of real engineering titles ("AI
Platforms Engineer", "Sr Manager, Software Development Engineering,
Workday Community", "Sr. Network Engineer") that don't literally contain
"software engineer"/"AI engineer"/etc. verbatim.

Date parsing: Workday returns relative strings ("Posted Yesterday",
"Posted 3 Days Ago", etc.) — converted to YYYY-MM-DD the same way as
browserstack_fetcher.py / simcorp_fetcher.py / genpact_fetcher.py.

Job detail: fetched from the CXS JSON detail endpoint at
`/wday/cxs/workday/Workday/job/<externalPath>` — returns
`jobPostingInfo.jobDescription` (HTML) and `jobPostingInfo.startDate`
(ISO-8601 posting date), identical shape to browserstack_fetcher.py.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""

_BASE_URL = "https://workday.wd5.myworkdayjobs.com"
_TENANT = "workday"
_SITE = "Workday"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"

_MAX_LIMIT = 20  # Workday hard-caps at 20 for this tenant (>20 -> HTTP 400)

# Confirmed live facet value id for the India country facet.
_INDIA_COUNTRY_FACET_ID = "c4f78be1a8f14da0ab49ce1162348a5e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_JOB_BASE}",
}

# Description cache: application_url -> (description, posting_date)
_desc_cache: dict[str, tuple[str, str]] = {}

# Pagination-wraparound guard
_FIRST_PAGE_IDS: set[str] | None = None


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


def _normalize_location(loc_text: str) -> str:
    """Normalise this tenant's "IND.Pune" / "India, Mumbai" / "3 Locations"
    shapes to "<City>, India". The India country facet already guarantees
    every result passed here is genuinely India-located.
    """
    loc = (loc_text or "").strip()
    if not loc:
        return "India"
    if "india" in loc.lower():
        return loc
    if loc.upper().startswith("IND."):
        city = loc[4:].strip()
        return f"{city}, India" if city else "India"
    # Ambiguous multi-location strings (e.g. "3 Locations") — can't recover
    # a real city, but the facet still guarantees India.
    return "India"


def _job_id_from_external_path(external_path: str) -> str:
    """Extract the Workday requisition ID from an externalPath, mirroring
    browserstack_fetcher.py's fallback (bulletFields[0] is preferred)."""
    m = re.search(r"_([A-Za-z]+-?\d+)$", external_path)
    return m.group(1).upper() if m else ""


def _post_with_retries(
    url: str, body: dict, timeout: int, label: str
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(url, headers=_HEADERS, json=body, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Workday {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Workday {label} failed: {exc}") from exc
    raise RateLimitError(f"Workday {label}: no response — {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return one page of Workday's (the company's) own Workday CXS job
    listings.

    `searchText` is applied server-side (genuine keyword filter). India
    is scoped server-side via the `Location_Country` facet — a reliable
    real facet on this tenant, unlike most Workday tenants already in
    this repo. `limit` is clamped to 20 (Workday hard cap for this
    tenant).
    """
    global _FIRST_PAGE_IDS

    body = {
        "appliedFacets": {"Location_Country": [_INDIA_COUNTRY_FACET_ID]},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    r = _post_with_retries(_SEARCH_URL, body, timeout, "search")

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = _normalize_location(p.get("locationsText") or "")

        external_path = p.get("externalPath", "")
        bullet_fields = p.get("bulletFields") or []
        job_id = (bullet_fields[0] if bullet_fields else "") or _job_id_from_external_path(external_path)
        if not job_id:
            continue

        title = (p.get("title") or "").strip()
        if not title:
            continue

        posting_date = _parse_posted_on(p.get("postedOn", ""))
        application_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    # Pagination-wraparound guard
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    if f"{_JOB_BASE}/" in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Workday description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Workday description fetch failed: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"Workday description: no response — {last_exc}")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )
    posting_date = (info.get("startDate") or "")[:10]

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
