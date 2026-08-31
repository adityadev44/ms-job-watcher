"""Fetches UiPath job listings via the Ashby public job-board REST API.

UiPath's careers site (www.uipath.com/careers) links out to
jobs.ashbyhq.com/uipath — the company is on Ashby, a new ATS vendor not
previously integrated in this repo. Ashby exposes a public, unauthenticated
job-board endpoint:

    GET https://api.ashbyhq.com/posting-api/job-board/uipath

Verified live 2026-08-31 — returns the entire board (119 jobs across all
UiPath offices worldwide) in a single call, no pagination and no query
params: `{"jobs": [...], "apiVersion": ...}`. No server-side keyword or
location filtering exists, so — same pattern as CRED/Meesho/Paytm (Lever)
and Groww/Razorpay (Greenhouse) — the whole board is fetched once, cached
in-module, and sliced/filtered from there.

Description: unlike Lever's `descriptionPlain` (intro blurb only, real
content lives in a separate `lists` array), Ashby's `descriptionPlain` is
already the FULL plain-text job description — verified its length matches
HTML-tag-stripped `descriptionHtml` almost exactly (7007 vs 6940 chars for
a sampled posting, the gap being just tag whitespace). No HTML stripping or
section concatenation needed; `descriptionPlain` is used as-is.

Location: Ashby's top-level `location` string is sometimes an internal
office label ("Bangalore - Engineering") rather than a clean city name, and
never contains "India" as a substring — same problem as Lever boards. Each
job instead carries a structured `address.postalAddress` object with
`addressLocality` ("Bengaluru")/`addressRegion` ("Karnātaka" — has a stray
diacritic, avoided)/`addressCountry` ("India"). Location text handed to
matcher.py is built as "{addressLocality}, {addressCountry}" whenever
addressCountry is present, falling back to the raw `location` field
otherwise — this both gives is_india_job() a genuine "India" substring to
match and sidesteps the diacritic-corrupted region field.

Titles: UiPath is a direct AI-automation product employer with a small,
genuine (not IT-services-generic) India engineering presence — live titles
are "Senior Software Engineer", "Principal Software Engineer", "Senior
Forward Deployed Scientist", "Solution Architect", "Technical Account
Manager II", "Principal Product Manager" (2026-08-31 snapshot, 6 India
postings). These carry real signal, and the description bodies name actual
stack details in the first few hundred characters (.NET Core, Angular,
Kubernetes, Azure; agentic/GenAI platform work) rather than boilerplate —
no generic level-banded titles hiding the tech stack the way HCLTech/Wipro
do. require_tech_in_description is therefore NOT enabled for this company,
consistent with other direct-employer product companies (LSEG, Broadridge,
SAP Labs) rather than the IT-services shops that need Layer 4.

Caching: `_cache_filled = True` is set before the request attempt so a
transient failure doesn't retry on every fetch_jobs() call in the same
process run (Honeywell/Persistent/CRED "avoid a retry storm" lesson).
Keyword/location args are accepted but ignored — the shared matcher does
the real title/skill filtering after this fetcher hands back the full
board.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

_BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/uipath"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.uipath.com/careers",
}

# Module-level cache: Ashby's public board endpoint returns the identical
# full job list for every query — fetch it once and slice/look up after.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _parse_date(published_at: str) -> str:
    """Ashby's publishedAt is ISO-8601 ("2026-06-02T06:21:04.372+00:00");
    the first 10 characters are always YYYY-MM-DD."""
    if not published_at or len(published_at) < 10:
        return ""
    candidate = published_at[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
        return candidate
    except ValueError:
        return ""


def _location_from_job(job: dict) -> str:
    addr = (job.get("address") or {}).get("postalAddress") or {}
    country = (addr.get("addressCountry") or "").strip()
    locality = (addr.get("addressLocality") or "").strip()
    raw_location = (job.get("location") or "").strip()

    base = locality or raw_location or country
    if not base:
        return ""
    if country and country.lower() not in base.lower():
        return f"{base}, {country}"
    return base


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Ashby board once; cache job list + descriptions.

    _cache_filled is set before the request attempt so a transient failure
    doesn't trigger a retry storm on every fetch_jobs() call made during the
    same process run.
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_BOARD_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("UiPath Ashby board: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"UiPath board fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"UiPath board fetch: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"UiPath board fetch: invalid JSON — {exc}") from exc

    postings = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(postings, list):
        return

    jobs: list[dict] = []
    for p in postings:
        if p.get("isListed") is False:
            continue
        job_id = str(p.get("id") or "").strip()
        title = (p.get("title") or "").strip()
        job_url = p.get("jobUrl") or p.get("applyUrl") or ""
        if not (job_id and title and job_url):
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(p),
            "posting_date": _parse_date(p.get("publishedAt") or ""),
            "application_url": job_url,
        })
        _desc_cache[job_id] = (p.get("descriptionPlain") or "").strip()

    _job_cache[:] = jobs
    print(f"[UiPath] Cache filled: {len(jobs)} jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of UiPath (Ashby) postings.

    Keyword/location are accepted but ignored — Ashby's public job-board
    endpoint returns the identical full board regardless of query params;
    the shared matcher does the real title/skill filtering. The whole board
    is cached once and sliced here.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    """jobUrl is .../uipath/{id}[?query] or .../uipath/{id}/application."""
    path = (application_url or "").split("?", 1)[0].rstrip("/")
    if path.endswith("/application"):
        path = path[: -len("/application")]
    return path.rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single UiPath job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    Ashby's board response already carries the full plain-text description,
    so there is no separate detail endpoint to call.
    """
    _fill_cache(timeout=timeout)
    job_id = _job_id_from_url(application_url)
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
