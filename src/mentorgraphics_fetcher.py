"""Fetches Siemens EDA (formerly Mentor Graphics) job listings from
jobs.sw.siemens.com — the Siemens Digital Industries Software careers site.

ATS identification (Step 1, verified live 2026-09-14, not guessed): Mentor
Graphics was rebranded "Siemens EDA" after Siemens' 2017 acquisition and now
sits under the "Siemens Digital Industries Software" business unit, whose
careers site (`jobs.sw.siemens.com`) is a **genuinely separate** pipeline
from `jobs.siemens.com` (the generic Siemens AG iCIMS portal this repo
already scrapes via `siemens_fetcher.py` for Buildings/Mobility/Smart
Infrastructure roles — confirmed at investigation time that Siemens
Mobility's postings live entirely on that other, already-covered portal).
`jobs.sw.siemens.com` is a client-rendered Nuxt.js SPA with no server-side
job data in its raw HTML; captured its real XHR via a headless-browser
network trace (curl/requests alone would see an empty shell) — the actual
job-search backend is `prod-search-api.jobsyn.org`, a "jobsyn.org"/NLX
job-distribution API, a vendor not previously seen in this repo. The
request requires an `x-origin: jobs.sw.siemens.com` header (a page-view
CSP `connect-src` line embeds the exact required header value) — a plain
`Origin`/`Referer` pair alone gets a `{"errors": "Mismatched origin."}`
403-equivalent response.

`GET https://prod-search-api.jobsyn.org/api/v1/solr/search?page=N&location=ind`
returns full JSON with plain-text (markdown-ish) descriptions embedded
inline — no separate detail fetch needed. `num_items` is accepted but
silently ignored (always 10/page); the response's own `pagination.total`/
`total_pages` fields are the reliable pagination signal. 57 India jobs
across 6 pages at verification time (`location=ind`, ISO-3166 alpha-3 code,
confirmed via a live network capture of the site's own India-filtered page,
not guessed).

Live data check (2026-09-14): unlike the chip-design GCCs (AMD/Intel/
Broadcom/TI/Arm) already in this repo, Siemens EDA/Digital Industries
Software's India pool (Noida/Bengaluru/Pune/Hyderabad/Chennai/Mumbai — NOT
Pune-only) is majority genuine *software* engineering: "Software
Development Engineer" (C/C++ compiler work for EDA, ML/AI mentioned as
"good to have"), "Senior Software Engineer", "Software Engineer – C++ and
Python", "Software QA Engineer - Advanced, Test Automation & AI", "SRE
Engineer", "Member Technical Staff". A real EDA business-structure facet
(`electronic-design-automation-eda`, 12 of 57 jobs) confirms Siemens EDA
postings are genuinely present in this pool, not just generic Siemens
Digital Industries Software roles.
"""
from __future__ import annotations

import re
import time

import requests

_API_URL = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
_SITE_BASE = "https://jobs.sw.siemens.com"
_LOCATION_CODE = "ind"  # India, ISO-3166 alpha-3, confirmed via live network capture
_MAX_PAGES = 20  # safety cap

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_SITE_BASE}/",
    "Origin": _SITE_BASE,
    "x-origin": "jobs.sw.siemens.com",
}

_cache: list[dict[str, str]] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled = False


class RateLimitError(Exception):
    pass


def _city_slug(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", city.strip().lower()).strip("-")


def _application_url(job: dict) -> str:
    city = job.get("city_exact") or ""
    slug = job.get("title_slug") or ""
    guid = job.get("guid") or ""
    if city and slug and guid:
        return f"{_SITE_BASE}/{_city_slug(city)}-ind/{slug}/{guid}/job/"
    return f"{_SITE_BASE}/jobs/"


def _fill_cache(timeout: int) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before the fetch loop -- avoid a retry storm on failure

    for page in range(1, _MAX_PAGES + 1):
        params = {"page": page, "location": _LOCATION_CODE, "num_items": 10}
        r = None
        for attempt in range(3):
            try:
                r = requests.get(_API_URL, headers=_HEADERS, params=params, timeout=timeout)
                if r.status_code == 429:
                    raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise RateLimitError(
                        f"Siemens EDA search failed after 3 attempts (page={page}): {exc}"
                    ) from exc
                time.sleep(2 ** attempt)

        data = r.json()
        raw_jobs = data.get("jobs", [])
        if not raw_jobs:
            break

        for j in raw_jobs:
            guid = j.get("guid") or ""
            if not guid:
                continue

            title = (j.get("title_exact") or "").strip()
            loc_exact = j.get("location_exact") or ""
            city = j.get("city_exact") or ""
            location = f"{city}, India" if city else (loc_exact or "India")

            raw_date = j.get("date_added") or j.get("date_new") or ""
            posting_date = raw_date[:10] if raw_date else ""

            app_url = _application_url(j)
            description = (j.get("description") or "").strip()

            _desc_cache[app_url] = (description, posting_date)
            _cache.append({
                "id": guid,
                "title": title,
                "location": location,
                "posting_date": posting_date,
                "application_url": app_url,
            })

        pagination = data.get("pagination", {})
        total_pages = pagination.get("total_pages", page)
        if page >= total_pages or not pagination.get("has_more_pages", False):
            break


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of Siemens EDA's cached India job pool.

    This tenant's search API has no confirmed keyword-narrowing behavior
    tested during onboarding (only the location filter was verified live),
    so the fetcher caches the full ~57-job India pool once and slices it
    locally, same "cache once, slice locally" discipline as AMD/Pepsico.
    """
    _fill_cache(timeout)
    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    if application_url in _desc_cache:
        return _desc_cache[application_url]
    return "", ""
