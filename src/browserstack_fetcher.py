"""
BrowserStack job fetcher — Workday public CXS REST API.

Careers site: https://www.browserstack.com/careers
Workday tenant: browserstack.wd3.myworkdayjobs.com, site: External
Confirmed live (2026-09-05) via plain POST to the CXS endpoint — no
Playwright needed.

Search endpoint: POST https://browserstack.wd3.myworkdayjobs.com/wday/cxs/browserstack/External/jobs
  Body: {"appliedFacets": {}, "limit": <n>, "offset": <start>, "searchText": "<keyword>"}
  - `searchText` is a genuine server-side filter (0 results for a nonsense
    token), so keywords genuinely narrow the result set.
  - No usable location facet exists — the board only exposes
    jobFamilyGroup/workerSubType/timeType/locationMainGroup, none of which
    have an India-specific sub-value. India filtering happens client-side
    via `locationsText`.
  - Server hard-caps `limit` at 20 (requesting 25+ returns HTTP 400). All
    requests are defensively clamped to 20.
  - Pagination wraps around past the true total (standard Workday behavior
    for small tenants) — terminated by the `_FIRST_PAGE_IDS` guard.

Live-verified 2026-09-05:
- 30 total postings, all in India (Mumbai Remote and Mumbai WFO) except a
  handful of US-remote Enterprise Account Manager roles on page 2.
- The current board is mostly Sales/Operations roles (no Software Engineer/
  .NET/AI titles visible today) — a genuine current fact, not a fetcher
  defect. Engineering roles are expected to appear when BrowserStack opens
  India-based engineering positions.
- India detection: "india" substring check on `locationsText` (all Mumbai
  entries already contain "Mumbai" but not "india" literally — a city
  allowlist is used instead, same pattern as SimCorp/Genpact).

Date parsing: Workday returns relative strings ("Posted Yesterday", "Posted
3 Days Ago", etc.) — converted to YYYY-MM-DD the same way as
simcorp_fetcher.py and genpact_fetcher.py.

Job detail: fetched from the CXS JSON detail endpoint at
`/wday/cxs/browserstack/External/job/<externalPath>` — returns
`jobPostingInfo.jobDescription` (HTML) and `jobPostingInfo.startDate`
(ISO-8601 posting date).
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

_BASE_URL = "https://browserstack.wd3.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/browserstack/External/jobs"
_JOB_BASE = f"{_BASE_URL}/External"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/browserstack/External"

_MAX_LIMIT = 20  # Workday hard-caps at 20 for this tenant (>20 -> HTTP 400)

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

# Known BrowserStack India office locations that appear in locationsText.
# "india" itself never appears verbatim in the locationsText field —
# all India roles show as "Mumbai Remote" or "Mumbai - WFO".
_INDIA_LOCATION_SUBSTRS = {"mumbai", "bengaluru", "bangalore", "hyderabad", "pune", "india"}

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


def _is_india_location(loc_text: str) -> bool:
    """Return True if locationsText indicates an India office."""
    lt = (loc_text or "").lower()
    return any(city in lt for city in _INDIA_LOCATION_SUBSTRS)


def _job_id_from_external_path(external_path: str) -> str:
    """Extract the Workday requisition ID from an externalPath.

    externalPath: '/job/Mumbai-Remote/Senior-Lead---..._JR103514'
    bulletFields[0]: 'JR103514'  (preferred, but extracted from path as fallback)
    """
    m = re.search(r"_([A-Za-z]+\d+)$", external_path)
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
                raise RateLimitError(f"BrowserStack {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"BrowserStack {label} failed: {exc}") from exc
    raise RateLimitError(f"BrowserStack {label}: no response — {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return one page of BrowserStack Workday job listings.

    `searchText` is applied server-side (genuine keyword filter). India
    filtering is client-side via locationsText substring matching.
    `limit` is clamped to 20 (Workday hard cap for this tenant).
    """
    global _FIRST_PAGE_IDS

    body = {
        "appliedFacets": {},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    r = _post_with_retries(_SEARCH_URL, body, timeout, "search")

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = (p.get("locationsText") or "").strip()
        if not _is_india_location(loc):
            continue

        external_path = p.get("externalPath", "")
        # Prefer bulletFields[0] as the req ID; fall back to path extraction.
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

    # Derive the CXS detail URL from the public job URL.
    if f"{_JOB_BASE}/" in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/External/", 1)[-1]
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
                raise RateLimitError("BrowserStack description: 429 rate-limited")
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
                f"BrowserStack description fetch failed: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"BrowserStack description: no response — {last_exc}")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )
    posting_date = (info.get("startDate") or "")[:10]

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
