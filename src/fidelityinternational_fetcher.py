"""Fetches Fidelity International job listings via the Workday public REST API.

Fidelity International (FIL) is a distinct legal entity from Fidelity
Investments/FMR (already tracked in this repo as ``fidelity`` /
``fidelity_fetcher.py`` — that is a separate US-based company and is not
touched here). FIL is the standalone international business that was spun
off from FMR in 1980 and has its own separate India presence.

ATS identified from scratch (not assumed) by loading
``https://careers.fidelityinternational.com`` and reading the outbound
links in the rendered HTML: the site links out to
``https://fil.wd3.myworkdayjobs.com/en-US/001`` — Workday, tenant ``fil``,
site ``001``. Standard Workday CXS REST API, same shape as Wells
Fargo/Citi/Alegeus/etc. in this repo — plain ``requests``, no Playwright.

Verified via direct A/B requests against the live API (2026-09-08):
- The tenant exposes a standard nested ``locationCountry`` facet (under
  ``locationMainGroup``) with the same cross-tenant India WID already used
  by Wells Fargo/Citi/Fidelity/Northern Trust/MUFG/Shell/Guidewire in this
  repo: ``c4f78be1a8f14da0ab49ce1162348a5e``. Applying it server-side
  returned 26/26 India results (offices: FIL Bengaluru Office, Gurgaon
  Office), out of an 84-job global pool.
- ``searchText`` genuinely narrows server-side: an empty query returns 26
  India jobs, ``.NET`` narrows to 5 — confirmed via direct A/B requests, so
  this fetcher is NOT registered in ``_IGNORES_KEYWORDS``.
- Job descriptions are NOT inline in the search response — fetched from the
  Workday CXS JSON detail API, same as Wells Fargo, which also returns a
  real ISO ``startDate`` used as the posting-date proxy.
- **Quirk vs. Wells Fargo/most other tenants here**: this tenant's search
  response has no ``locationsText``/``postedOn`` fields at all — each
  posting is just ``title``, ``externalPath``, and a ``bulletFields`` list
  (office name, "J##### Title (Open)", "Posting Date: DD/MM/YYYY" — note
  day-first format, verified against today's date). Location and posting
  date are parsed out of ``bulletFields`` instead of the usual top-level
  fields.

Live sample titles confirmed real India engineering roles: "Senior Data
Engineer", "Senior Analyst Programmer", "AI Test Engineer - Test Lead",
"Data Engineer", "Production Services Senior Analyst Programmer - Trading
Platform" — all Bengaluru or Gurgaon.
"""

from __future__ import annotations

import re
import time
import warnings

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://fil.wd3.myworkdayjobs.com"
_TENANT_PATH = "en-US/001"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/fil/001/jobs"
_JOB_BASE = f"{_BASE_URL}/{_TENANT_PATH}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/fil/001"
_PAGE_SIZE = 20

# India locationCountry WID for Fidelity International's Workday tenant —
# the same stable cross-tenant GUID used by several other Workday tenants
# already in this repo. Verified 2026-09-08: 26 India results out of an
# 84-job global pool.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Recognised India office-name tokens for the bulletFields[0] primary-office
# text. See the leakage note in fetch_jobs(): the India facet on this
# tenant also matches jobs whose primary office is elsewhere, so this
# allowlist is the real India-primary gate.
_INDIA_CITIES = ("india", "bengaluru", "bangalore", "gurgaon", "gurugram")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/{_TENANT_PATH}",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# Pagination on this tenant wraps around once offset reaches the true total
# (confirmed live: offset=26 on a 26-job India pool returns page 1 again
# instead of an empty list — same wraparound family as UBS/Nvidia/Walmart/
# Sprinklr elsewhere in this repo). Tracking already-seen IDs makes a
# wrapped page come back fully empty so matcher.py's `if not page: break`
# terminates pagination cleanly.
_seen_job_ids: set[str] = set()


# ---------------------------------------------------------------------------
# Date helper — bulletFields carries "Posting Date: DD/MM/YYYY"
# ---------------------------------------------------------------------------

def _parse_bullet_date(bullet_fields: list[str]) -> str:
    """Parse "Posting Date: DD/MM/YYYY" out of bulletFields (day-first)."""
    for field in bullet_fields:
        m = re.match(r"^Posting Date:\s*(\d{2})/(\d{2})/(\d{4})$", field.strip())
        if m:
            dd, mm, yyyy = m.groups()
            return f"{yyyy}-{mm}-{dd}"
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
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
        # This tenant returns HTTP 400 for limit > 20 (same ceiling seen on
        # several other Workday tenants in this repo, e.g. Nvidia/Walmart).
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
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Fidelity International Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Fidelity International fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")
        bullet_fields = p.get("bulletFields", []) or []

        # Job ID: Fidelity International uses "Jnnnnn" requisition numbers,
        # visible in bulletFields (e.g. "J70066 Analyst Programmer (Open)")
        # and as a suffix on externalPath. Fall back to externalPath itself
        # if no clean ID can be extracted.
        job_id = ""
        for field in bullet_fields:
            m = re.match(r"^(J\d+)\b", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(J\d+(?:-\d+)?)$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            job_id = external_path

        if job_id in _seen_job_ids:
            # Already returned in an earlier page this run -- either a true
            # duplicate or a wraparound page. Skip so a wrapped page comes
            # back fully empty and matcher.py's pagination loop terminates.
            continue
        _seen_job_ids.add(job_id)

        title = p.get("title", "").strip()
        if not (job_id and title):
            continue

        # No locationsText field on this tenant — the office name is the
        # first bulletFields entry (e.g. "Gurgaon Office", "FIL Bengaluru
        # Office"). IMPORTANT: the locationCountry=India facet on this
        # tenant is NOT primary-location-only — verified live that it also
        # matches jobs whose PRIMARY office is elsewhere (e.g. "Cannon
        # Street Office"/London, "Dalian Office"/China) as long as India
        # appears in the job's `additionalLocations`. Trusting the facet
        # alone would leak non-India-primary jobs into the India feed, so
        # bulletFields[0] (the real primary office) is checked against a
        # known-India-city allowlist and anything else is dropped here,
        # not passed through for matcher.py to sort out.
        loc = bullet_fields[0].strip() if bullet_fields else ""
        if not any(city in loc.lower() for city in _INDIA_CITIES):
            continue
        if "india" not in loc.lower():
            loc = f"{loc}, India"

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_bullet_date(bullet_fields),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_TENANT_PATH}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
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
                raise RateLimitError("Fidelity International description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Fidelity International description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
