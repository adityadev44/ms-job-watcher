"""Fetches Uniphore job listings via the Workday public REST API.

ATS discovery (live, 2026-09-08): Uniphore's own careers link (from
uniphore.com/careers) points at Workday. Two Workday hosts exist for
this company — ``uniphore.wd1.myworkdayjobs.com`` (site "Uniphore")
returns HTTP 422 for every site/tenant combination tried, while
``uniphore.wd503.myworkdayjobs.com`` (site "Uniphore") answers cleanly —
confirmed live via a plain POST to the CXS jobs endpoint. Same failure
shape as BNY Mellon/other multi-host Workday tenants in this repo: probe
before assuming the first host found via search results is correct.

India is scoped server-side via the standard cross-tenant
``locationCountry`` WID (``c4f78be1a8f14da0ab49ce1162348a5e``, the same
GUID reused by Wells Fargo/Citi/Fidelity/Sprinklr/Guidewire elsewhere in
this repo).

**Real, load-bearing gotcha found live**: this tenant's list-response
``locationsText`` is frequently the unhelpful placeholder ``"2
Locations"`` (Maersk-style) rather than a real place name — every India
posting observed today is dual-listed "India - Bangalore" +
"India - Chennai". Naively keeping ``locationsText`` as-is would either
(a) hide the real city entirely (breaking Chennai exclusion) or (b)
falsely exclude a genuinely Bangalore-primary posting merely because
Chennai is listed as an alternate option. Fixed by fetching each job's
detail record (cheap — the India pool is only ~4 jobs) and using its
``location`` field, which is the single authoritative *primary* site
(confirmed live: all 4 current India postings have ``location: "India -
Bangalore"`` with Chennai only in the secondary ``additionalLocations``
list) — so Chennai-only alternates never trigger the exclude-locations
check for a Bangalore-primary role, while a role whose own primary
``location`` is genuinely Chennai still gets it correctly.

Detail responses also carry the full ``jobDescription`` HTML inline, so
descriptions are cached during the same detail fetch — no separate
description round-trip needed at match time.

Server-side filtering: ``searchText`` genuinely narrows results (Workday
convention already documented across this repo for other tenants) — not
added to ``_IGNORES_KEYWORDS``.

Current live state (2026-09-08): 4 India postings, all Bangalore-primary
(Senior Staff Software Engineer (SDET), VP Product Engineering, Senior
Security Engineer, Principal Security Engineer) out of 39 global —
genuine AI-heavy JDs (RAG/LangChain/Generative AI referenced directly in
the SDET posting), a good match for this repo's AI/ML/Python track.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests

_BASE_URL = "https://uniphore.wd503.myworkdayjobs.com"
_TENANT = "uniphore"
_SITE = "Uniphore"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"

# NOTE: the standard cross-tenant locationCountry India WID
# (c4f78be1a8f14da0ab49ce1162348a5e) is BROKEN on this tenant — applying
# it as a locationCountry facet silently returns the full 39-job global
# pool, not just India (confirmed live, same failure class as
# Micron/Verizon/Lowe's/FICO elsewhere in this repo). Worked around with
# the tenant's city-level `locations` facet instead (Bangalore + Chennai
# WIDs), cross-verified live: 4 India jobs vs. 39 unfiltered.
_INDIA_LOCATION_WIDS = [
    "a9915d38cb07100cb34d33ce139d0000",  # India - Bangalore
    "a9915d38cb07100cb34d01e7ecbe0000",  # India - Chennai
]
_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": _JOB_BASE,
}

# Description cache keyed by application_url, filled during fetch_jobs()'s
# per-job detail lookups so fetch_job_description() is a free cache hit.
_detail_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Uniphore's Workday tenant."""


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


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return " ".join(text.split())


def _post_json(url: str, body: dict, *, timeout: int, context: str) -> dict:
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(url, headers=_HEADERS, json=body, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Uniphore {context}: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Uniphore {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Uniphore {context}: no response")


def _get_json(url: str, *, timeout: int, context: str) -> dict:
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Uniphore {context}: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Uniphore {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Uniphore {context}: no response")


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
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }
    data = _post_json(_SEARCH_URL, body, timeout=timeout, context="job list")

    jobs: list[dict] = []
    for p in data.get("jobPostings", []):
        external_path = p.get("externalPath", "")
        if not external_path:
            continue

        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(JR\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(JR\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        app_url = f"{_JOB_BASE}{external_path}"

        # locationsText is often the unhelpful "N Locations" placeholder
        # (verified live) — resolve the real *primary* site (and cache the
        # inline description for free) via the per-job detail record. The
        # India pool here is tiny (~4 jobs), so this per-job call is cheap.
        loc = p.get("locationsText", "").strip()
        description = ""
        posting_date = _parse_posted_on(p.get("postedOn", ""))
        try:
            detail = _get_json(
                f"{_DETAIL_BASE}{external_path}",
                timeout=timeout,
                context=f"job detail {job_id}",
            )
            info = detail.get("jobPostingInfo", {})
            primary_loc = (info.get("location") or "").strip()
            if primary_loc:
                loc = primary_loc
            description = _strip_html(info.get("jobDescription") or "")
            if info.get("startDate"):
                posting_date = info["startDate"]
        except RateLimitError:
            # Fall back to the (possibly ambiguous) list-response location
            # rather than failing the whole page; description stays empty
            # and will be re-fetched by fetch_job_description() below.
            pass

        if "india" not in loc.lower():
            continue

        _detail_cache[app_url] = (description, posting_date)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc or "India",
            "posting_date": posting_date,
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Uniphore job.

    Usually a free cache hit — ``fetch_jobs`` already resolved every
    India job's detail record (to fix the "N Locations" ambiguity) and
    cached its inline description. Falls back to a fresh detail call
    only if the cache was never populated for this URL (e.g. called
    directly without a prior ``fetch_jobs`` pass).
    """
    cached = _detail_cache.get(application_url)
    if cached is not None:
        return cached

    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]

    data = _get_json(f"{_DETAIL_BASE}{ext_path}", timeout=timeout, context="job detail (direct)")
    info = data.get("jobPostingInfo", {})
    description = _strip_html(info.get("jobDescription") or "")
    posting_date = info.get("startDate") or _parse_posted_on(info.get("postedOn", ""))
    _detail_cache[application_url] = (description, posting_date)
    return description, posting_date
