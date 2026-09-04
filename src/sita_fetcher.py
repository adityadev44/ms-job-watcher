"""
SITA job fetcher — iCIMS REST API via Jibe Careers Site Builder
(careers.sita.aero).

SITA (air-transport IT/communications: sita.aero) looked like it could be a
bespoke/custom careers site at a glance (its own branded domain,
`careers.sita.aero`), but a real DevTools-style check (view-source + a
direct hit on the search endpoint) shows the same "Jibe" front-end CDN
assets (``app.jibecdn.com`` / ``assets.jibecdn.com``) already seen at
PepsiCo/Schneider Electric/HealthEdge, and the search API response's own
``"ats_code": "icims"`` field plus every ``apply_url`` pointing at
``careers-sita.icims.com`` confirms it. Same underlying REST API family as
``pepsico_fetcher.py``, ``schneiderelectric_fetcher.py``,
``healthedge_fetcher.py``, ``gallagher_fetcher.py``, and
``spglobal_careers_fetcher.py`` (``GET /api/jobs``, no auth required). This
is a fresh from-scratch verification, not a reused guess — earlier waves in
this repo found "custom ATS" guesses from secondary research wrong the
majority of the time (see PLAYBOOK.md's Key Bugs table).

Verified via direct A/B requests against the live API (not assumed):
- ``location=India`` genuinely filters server-side (facet count of 63
  matches the number of jobs returned when explicitly requesting India).
- ``keywords`` also genuinely narrows server-side (e.g. ``keywords=Finance``
  returns ``totalCount: 14`` vs. 63 unfiltered) — but this fetcher
  deliberately ignores it and always fetches the *full* India pool in one
  shot instead, same choice already made for HealthEdge/MetLife/Infosys
  ("the whole pool fits in one request; title/skill filters handle the
  rest"). Registered in ``_IGNORES_KEYWORDS`` for this reason.
- ``offset`` is silently ignored: offset=10&limit=5 returns the identical
  first 5 jobs as offset=0 -- the same broken-pagination shape already seen
  at PepsiCo/HealthEdge on this same ATS family. Irrelevant here anyway
  since the whole India pool (63 jobs today) fits under the single-page
  ``limit`` cap.
- ``limit`` hard-caps at 100 -- a request above that returns HTTP 422 (the
  same "422 over cap" variant as Schneider Electric/HealthEdge's tenants,
  not PepsiCo's "200 with a generic error body" variant). 100 comfortably
  covers today's 63-job India pool.

Full job description is embedded directly in the search response as a
single plain-text ``description`` field (no HTML tags observed, unlike
PepsiCo/Schneider Electric's tenants) -- ``fetch_job_description()`` is
served entirely from an in-module cache built during ``fetch_jobs()``. A
separate ``qualifications`` field also exists on most postings but its text
is always a subset already contained in ``description`` -- not concatenated
separately (unlike PepsiCo, where the two fields carry distinct content).

``apply_url`` (``careers-sita.icims.com/jobs/{id}/login``) returns HTTP 405
for plain HTTP traffic -- confirmed via direct probe, the same protection
class already documented for HealthEdge/Schneider Electric/IBM's job-detail
pages. Alerts link to the public, unauthenticated
``careers.sita.aero/jobs/{id}?lang=en-us`` page instead, confirmed via
direct fetch to render the correct job (matching ``<title>``) with no login
wall.

Live-verified 2026-09-04: 63 India postings across Delhi (32), Mumbai (15),
Bengaluru (11), Vadodara/Surat/Ahmedabad/Jaisalmer (1 each), plus one
Dubai-primary posting whose ``full_location`` also names Delhi, India (kept
-- ``full_location`` already contains the "India" substring matcher.py's
``is_india_job()`` needs). None of SITA's India cities collide with the
shared ``exclude_locations`` defaults (no Chennai/Pune/Tamil Nadu/Kochi
postings observed). Real, current ``.NET / C#`` matches exist today in
Bengaluru: "Senior Software Developer (C#)" and "Lead Software Developer
(C#)" postings explicitly require "at least 5 years of experience in C#...
.Net Core" in their description bodies -- exactly the 5-7-year-band .NET/C#
profile this watcher targets. Titles here name the stack directly in
parentheses (not generic level-banded IT-services titles), so
``require_tech_in_description`` was deliberately left off.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://careers.sita.aero"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/jobs",
}

# Server hard-caps `limit` at 100 (a higher value returns HTTP 422, like
# Schneider Electric/HealthEdge's tenants) -- comfortably above today's
# 63-job India pool.
_MAX_PAGE_SIZE = 100


class RateLimitError(Exception):
    pass


# Populated once by the first fetch_jobs() call; every later call (any
# keyword -- keywords are ignored, see module docstring) slices this list
# locally instead of re-querying. Same cache-once shape as
# healthedge_fetcher.py / hexaware_fetcher.py.
_india_jobs_cache: list[dict] | None = None
# application_url -> (description, posting_date), populated alongside the cache.
_desc_cache: dict[str, tuple[str, str]] = {}


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-08-17T12:40:00+0000' -> '2026-08-17'."""
    return raw[:10] if raw else ""


def _location_str(j: dict) -> str:
    """Prefer the combined location field -- already "City, India" on this
    tenant, including for the one observed multi-location (Dubai + Delhi)
    posting, so no client-side ", India" append is needed here (unlike
    several Workday tenants elsewhere in this repo).
    """
    loc = (j.get("full_location") or j.get("short_location") or "").strip()
    if loc:
        return loc
    city = (j.get("city") or "").strip()
    country = (j.get("country") or "").strip()
    if city or country:
        return f"{city}, {country}".strip(", ")
    return ""


def _fetch_india_pool(timeout: int) -> list[dict]:
    """Hit the real API once for the full India job pool and return parsed dicts.

    Always requests location=India, limit=100, offset=0 -- keywords are
    deliberately never sent (see module docstring: this fetcher is
    registered in _IGNORES_KEYWORDS), and offset is broken anyway so there
    is no benefit to varying it.
    """
    params = {
        "location": "India",
        "limit": _MAX_PAGE_SIZE,
        "offset": 0,
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"SITA search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    try:
        raw_jobs = r.json().get("jobs", [])
    except ValueError as exc:
        raise RateLimitError(f"SITA search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        loc = _location_str(j)
        posting_date = _parse_date(j.get("posted_date", ""))
        # apply_url (careers-sita.icims.com/jobs/{id}/login) returns HTTP 405
        # for plain HTTP traffic -- link to the public, unauthenticated
        # careers.sita.aero job page instead.
        application_url = f"{_BASE_URL}/jobs/{job_id}?lang=en-us"

        description = _strip_html(j.get("description", ""))
        _desc_cache[application_url] = (description, posting_date)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    return jobs


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of SITA's India job results.

    `keyword` is accepted but ignored (see module docstring -- registered in
    company_registry._IGNORES_KEYWORDS, so run_company.py only ever calls
    this once per run with a single placeholder keyword). The first call
    fetches and caches the full ~63-job India pool in one request; every
    later call slices the cached list locally.
    """
    global _india_jobs_cache
    if _india_jobs_cache is None:
        _india_jobs_cache = _fetch_india_pool(timeout)
    return _india_jobs_cache[start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) -- served from the cache fetch_jobs() built.

    Falls back to a live HTML fetch of the public job page only if the cache
    is somehow missing the URL (should not normally happen).
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(
                application_url, headers={**_HEADERS, "Accept": "text/html"}, timeout=timeout
            )
            if r.status_code == 429:
                raise RateLimitError(f"429 on {application_url}")
            r.raise_for_status()
            text = _strip_html(r.text)
            result = (text, "")
            _desc_cache[application_url] = result
            return result
        except RateLimitError:
            raise
        except Exception:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    return "", ""
