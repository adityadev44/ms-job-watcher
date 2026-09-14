"""Fetches Hyundai Motor India Engineering (HMIE) job listings — custom
in-house AngularJS ATS at career.hmie.in.

ATS discovery (2026-09-13/14): HMIE (Hyundai Motor India Engineering Pvt
Ltd, the R&D/engineering-support centre in **Hyderabad**, NOT Pune) runs its
own hand-built careers portal — a legacy AngularJS 1.x SPA, not any
third-party ATS vendor. Its own webpack-free JS bundle
(`/js/jp/job-posting-*.js`) defines an `ngResource` factory pointed at a
plain REST backend:

    GET /api/service/jobposting/find?page=N
        -> {"content": [...], "totalElements": N, "size": 10, ...}
        (Spring Data-style pageable response; requires the header
        `X-Requested-With: XMLHttpRequest` or the backend 400s)
    GET /api/service/jobposting/findJob?id={id}
        -> full single-job JSON (same shape as one `content[]` entry)

No auth, no CSRF, no session — a cold `requests.get()` with just that one
header returns the same JSON the live Angular app gets. `mySkills`/`myLoc`/
`myJobFunction` filter params exist (read from the `find` service's own
resource definition) but were not needed here: the entire company-wide pool
is only ~2 open postings at investigation time, comfortably under one page,
so this fetcher ignores keyword/location and caches the whole pool once.

**Location, not from a flat string field** — each job's `jobLocationMappingSet`
is a list of `{location: {description: "Hyderabad"}}` entries (a bare city
name, no "India" substring at all, same shape as Continental/Lenskart/Maruti
Suzuki elsewhere in this repo) — `", India"` is appended before handing off
to matcher.py.

**Confirmed NOT Pune-only** (the specific concern this task was asked to
re-check): every job location observed across HMIE's live pool is
"Hyderabad" — HMIE's own R&D centre, established 2006, is HQ'd there, not in
Pune at all. HMIE currently has zero engineering/software titles in its tiny
live pool (both current postings are "Product Planning"/generic "Assistant
Manager" roles, level-banded, non-technical) — a real, working, feasible
pipeline with a genuine current-zero-match snapshot, same class of finding
as Ford/GM/Stellantis elsewhere in this repo, not a fetcher defect.

`jobTitleInUrl` gives a human-readable slug for constructing a working public
apply link (`https://career.hmie.in/#!/public/detailed-job-view/{id}` mirrors
the Angular route read out of the bundle's own `$routeParams.id > 0` guard;
the slug itself isn't part of the route but is kept for readability/logging).
"""

from __future__ import annotations

import re
import time

import requests

_BASE = "https://career.hmie.in"
_FIND_URL = f"{_BASE}/api/service/jobposting/find"
_FINDJOB_URL = f"{_BASE}/api/service/jobposting/findJob"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _locations_from(job: dict) -> str:
    locs = []
    for entry in job.get("jobLocationMappingSet") or []:
        desc = ((entry.get("location") or {}).get("description") or "").strip()
        if desc:
            locs.append(desc)
    loc_str = "; ".join(locs) if locs else ""
    if not loc_str:
        return ""
    if not re.search(r"\bindia\b", loc_str, re.IGNORECASE):
        loc_str = f"{loc_str}, India"
    return loc_str


def _parse_epoch_millis(value) -> str:
    if not value:
        return ""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


# Module-level cache: HMIE's entire pool fits on one page, so it's fetched
# once and reused for every keyword/location call in this process.
_cache: list[dict] = []
_descriptions: dict[str, tuple[str, str]] = {}
_cache_filled: bool = False


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _FIND_URL,
                params={"page": 0},
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("HMIE: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"HMIE fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"HMIE: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"HMIE: invalid JSON — {exc}") from exc

    collected: list[dict] = []
    for j in payload.get("content", []):
        job_id = str(j.get("id") or "").strip()
        title = (j.get("jobTitle") or j.get("jobDesignation") or "").strip()
        if not (job_id and title):
            continue

        loc_str = _locations_from(j)
        if not loc_str:
            continue

        posting_date = _parse_epoch_millis(j.get("createdTime"))
        app_url = f"{_BASE}/#!/public/detailed-job-view/{job_id}"

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_str,
            "posting_date": posting_date,
            "application_url": app_url,
        })
        _descriptions[job_id] = ((j.get("jobDescription") or "").strip(), posting_date)

    _cache[:] = collected
    print(f"[HMIE] Cache filled: {len(collected)} jobs company-wide")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of HMIE postings from the cached full pool.

    keyword/location are accepted for interface compatibility but ignored —
    the whole company-wide pool is tiny (~2 jobs at investigation time) and
    is cached once; matcher.py's shared filters do the real work.
    """
    _fill_cache(timeout=timeout)
    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) via the in-module cache filled
    during fetch_jobs(); falls back to a live findJob call if the id isn't
    cached (e.g. a fresh process resuming from seen_jobs.json).
    """
    m = re.search(r"/detailed-job-view/(\d+)", application_url or "")
    job_id = m.group(1) if m else ""
    if job_id and job_id in _descriptions:
        return _descriptions[job_id]

    if not job_id:
        return "", ""

    try:
        r = requests.get(
            _FINDJOB_URL,
            params={"id": job_id},
            headers=_HEADERS,
            timeout=timeout,
        )
        if r.status_code == 429:
            raise RateLimitError("HMIE description: 429 rate-limited")
        if r.status_code in (400, 404):
            return "", ""
        r.raise_for_status()
        data = r.json()
    except RateLimitError:
        raise
    except requests.RequestException as exc:
        raise RateLimitError(f"HMIE description fetch failed: {exc}") from exc
    except ValueError as exc:
        raise RateLimitError(f"HMIE description: invalid JSON — {exc}") from exc

    desc = (data.get("jobDescription") or "").strip()
    posting_date = _parse_epoch_millis(data.get("createdTime"))
    return desc, posting_date
