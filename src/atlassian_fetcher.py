"""
Atlassian job fetcher — first-party JSON proxy over an iCIMS ATS.

ATS discovery (2026-08-31): the public listing page
(https://www.atlassian.com/company/careers/all-jobs) is a Magnolia CMS
("wac") marketing page. Its raw server-rendered HTML has zero job content —
no __NEXT_DATA__, no inline JSON, no recognizable ATS string (no
greenhouse/lever/workday/icims/successfactors token anywhere in the initial
response). The listing only appears after client-side JS runs (confirmed via
a full Playwright/Firefox render + screenshot: a "Browse Jobs" grid with
~230 real postings across Sales/Engineering/Data/Support/etc. materializes
several seconds after load). Capturing the page's own network traffic (95
requests, most ad/analytics pixels) found exactly one relevant call:

    GET https://www.atlassian.com/endpoint/careers/listings

This is a first-party endpoint on atlassian.com itself (not a third-party
ATS domain) that proxies/caches the real backing ATS and returns the ENTIRE
current job pool — 234 postings observed live — as one flat JSON array, no
pagination, no auth, no query params applied server-side (verified: adding
`?q=engineer` returns byte-identical output). Each posting's own
`portalJobPost.portalUrl` field reveals the real ATS underneath:

    https://globalcareers-atlassian.icims.com/jobs/{id}/{slug}/job

confirming **iCIMS** (same ATS family as S&P Global Careers / Gallagher /
Charles Schwab already in this repo), fronted by Atlassian's own cache layer
so this fetcher never has to deal with iCIMS's session/CSRF quirks directly
— one GET returns everything, including full HTML description sections
(`overview`, `responsibilities`, `qualifications`) inline. No per-job detail
fetch is needed at all — same "inline descriptions" shape as S&P Global
Careers/Gallagher/Razorpay/CRED.

Quirks:
- Keyword/location query params are ignored server-side (confirmed above) —
  the full board is fetched once and cached in-module, same "cache-once,
  Honeywell-lesson `_cache_filled` set before the request" pattern as
  Groww/Razorpay/CRED/Persistent/MSCI. India filtering happens inside the
  cache fill (location strings already contain the literal word "India" —
  e.g. "Bengaluru - India -   Bengaluru,  560071 India" — so no city-name
  normalisation hack is needed here, unlike Razorpay/CRED/Lowe's).
- The feed contains exact full-record duplicate entries for a handful of
  job IDs (same id, title, locations — 234 raw rows, 219 unique ids observed
  live). Deduplicated by id when building the cache.
- `posting_date` comes from `portalJobPost.updatedDate`, already formatted
  as "YYYY-MM-DD HH:MM AM/PM" — the first 10 characters are the ISO date,
  no parsing needed.
- `application_url` uses `portalJobPost.portalUrl` (the canonical iCIMS job
  page, no query string) rather than the sibling `applyUrl` (same page with
  `?mode=apply` appended) — both resolve (HTTP 200, verified live), portalUrl
  is simpler to parse the job id back out of for fetch_job_description().
- Live-verified 2026-08-31: 27 of 234 global postings are India-located
  (all Bengaluru/Remote-India). Nearly every India Engineering-category
  title is "Principal ..." or "... Manager" (e.g. "Principal Backend
  Software Engineer", "Senior Machine Learning Systems Engineering Manager
  - AI & ML Platform") — both "principal" and "manager" are global
  `matching.exclude_terms`, so today's real India pool yields 0 matches
  through Layer 2 title filtering. This is a genuine current-zero, not a
  fetcher defect (same situation already documented for PhonePe/ING/eClerx)
  — a non-Principal, non-Manager "Software Engineer"/"Backend Engineer"
  posting in Bengaluru would flow through correctly the moment one opens.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_LISTINGS_URL = "https://www.atlassian.com/endpoint/careers/listings"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.atlassian.com/company/careers/all-jobs",
}

# Module-level cache: the listings endpoint returns the same full global
# pool regardless of query params, so it is fetched once and reused for
# every keyword/location call in this process (Honeywell/Persistent lesson:
# _cache_filled is set to True *before* the fetch attempt so a transient
# failure doesn't retry-storm on every subsequent fetch_jobs() call).
_india_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_date(updated_date: str) -> str:
    """'2026-08-18 08:11 AM' -> '2026-08-18'."""
    return (updated_date or "")[:10]


def _job_id_from_url(application_url: str) -> str:
    """'https://globalcareers-atlassian.icims.com/jobs/25480/...-mid-market/job' -> '25480'."""
    m = re.search(r"/jobs/(\d+)/", application_url or "")
    return m.group(1) if m else ""


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Atlassian job pool once and cache India postings.

    _cache_filled is set before the request attempt so a failure doesn't
    trigger a retry storm on every fetch_jobs()/fetch_job_description() call
    made during the same process run.
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_LISTINGS_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Atlassian listings: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Atlassian listings fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Atlassian listings fetch: no response — {last_exc}")

    try:
        raw_jobs = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Atlassian listings: invalid JSON — {exc}") from exc

    if not isinstance(raw_jobs, list):
        return

    seen_ids: set[str] = set()
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "").strip()
        title = (j.get("title") or "").strip()
        if not (job_id and title) or job_id in seen_ids:
            continue

        locations = j.get("locations") or []
        loc_str = "; ".join(str(loc).strip() for loc in locations if loc)
        if "india" not in loc_str.lower():
            continue
        seen_ids.add(job_id)

        portal = j.get("portalJobPost") or {}
        app_url = portal.get("portalUrl") or j.get("applyUrl") or ""
        posting_date = _parse_date(portal.get("updatedDate", ""))

        parts = [j.get("overview") or "", j.get("responsibilities") or "", j.get("qualifications") or ""]
        _desc_cache[job_id] = " ".join(_strip_html(p) for p in parts if p)

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_str or "India",
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _india_cache[:] = collected
    print(f"[Atlassian] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total global postings)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Atlassian India postings from the cached global pool.

    keyword/location are accepted for interface compatibility but ignored —
    the listings endpoint returns the identical full global pool regardless
    of query params (verified live); the shared matcher does the real
    title/skill filtering. The whole pool is cached once and sliced here.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Atlassian job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    the listings endpoint already includes full overview/responsibilities/
    qualifications HTML for every job, so no separate detail HTTP call is
    made.
    """
    _fill_cache(timeout=timeout)

    job_id = _job_id_from_url(application_url)
    description = _desc_cache.get(job_id, "")

    posting_date = ""
    for job in _india_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
