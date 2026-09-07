"""Fetches Rippling job listings via its own Algolia search index + its own
in-house ATS ("Rippling ATS" -- Rippling itself sells an ATS product and
dogfoods it for its own hiring).

ATS discovery (2026-09-06): rippling.com/careers/open-roles is a Next.js
page whose initial server-rendered payload carries NO job data at all (the
list is fetched client-side, `listMode: "pagination"`, `algoliaIndexName:
"careers_en-US_production"`). The Algolia Application ID and a public
search-only API key are hardcoded in one of the page's own JS bundles
(`_app-*.js`, plain string literals `"6FNAX3TBEF"` / `"416caa4690f002ff6fe4a2
097623640b"` next to references to `ALGOLIA_ENV`/`ALGOLIA_ADMIN_API_KEY` env
var *names* -- the literal values shipped to the client are the public
search key, not an admin secret, and are safe to call directly; standard
front-end Algolia integration pattern, same trust level as a Greenhouse
board token). Confirmed live via:

    POST https://6FNAX3TBEF-dsn.algolia.net/1/indexes/careers_en-US_production/query
    headers: X-Algolia-Application-Id, X-Algolia-API-Key
    body: {"query": "<text>", "hitsPerPage": N}

-- 682 total postings globally (2026-09-06). Real full-text search: an empty
query returns all 682; a nonsense token (`zzznonsensequeryabc123`) returns 0;
`"software engineer"` returns 136. Each hit's own `url` field
(`https://ats.rippling.com/rippling/jobs/{jobId}`) reveals the real detail-
page backend -- Rippling's own ATS product, not a third-party vendor.

**Location gotcha (why this fetcher does NOT rely on Algolia's own keyword
relevance to find India jobs)**: querying the index with `query: "India"`
looks tempting but returns a genuine false positive -- a real US posting
titled "Expansion Account Executive, Accountant Channel (Central)" located
in **Indianapolis, IN** matches because "Indianapolis" contains the literal
substring "india" (both in Algolia's own fuzzy-prefix relevance match, and
critically also in this repo's own substring-based `matcher.is_india_job()`
if the raw location string were ever passed through unfiltered). Instead,
this fetcher fetches the WHOLE board once (`hitsPerPage=1000`, paginating
via Algolia's `page` param past 1000 if the pool ever grows that large) and
filters using each hit's own structured `locations[].countryCode == "IN"`
field -- an authoritative signal that correctly excludes Indianapolis
(`countryCode: "US"`) while catching every real India posting. The India
location's own `name` field (e.g. "Bangalore, India", "Remote (India)")
already contains the literal "India" substring, so no further location
normalization is needed once the countryCode filter has run.

Verified live 2026-09-06: 93 unique India postings (of 682 global, before
countryCode filtering the "India" text-query approach above would have
returned 94 -- the one extra being the Indianapolis false positive) --
almost entirely Bangalore-based ("Remote (India)" appears once). Strong
genuine engineering signal: "Senior/Staff Software Engineer" (>15 combined,
across Backend/Frontend/Full Stack/HRIS/Payroll/Tax/Global Payroll/Rippling
AI), "Software Engineer II" (x4, incl. "(AI Governance)"), "Senior/Staff
Software Engineer - AI Governance", "Senior Staff Software Engineer -
Rippling AI", "Data Scientist, Financial Analytics", "Senior Frontend
Engineer - Web Infra", "Engineering Manager" (x2), "Senior Engineering
Manager" (x4), "Director Of Engineering" -- this is the strongest India
engineering signal of the four dev-tool/SaaS companies onboarded in this
batch.

**Per-job detail fetch**: the Algolia index carries no description or
posting date at all (title/department/location/url only), so
`fetch_job_description()` fetches the job's own detail page HTML directly
(no login/redirect, plain `requests` works) and parses the embedded
Next.js `__NEXT_DATA__` JSON blob for `apiData.jobPost.description`
(`{"company": "...", "role": "..."}` HTML fragments -- "role" carries the
actual responsibilities/requirements where the hard-skill terms live;
"company" is generic About-Rippling boilerplate, concatenated anyway for
consistency with this repo's Lever-fetcher convention of combining intro +
substantive bullets) and `apiData.jobPost.createdOn` (ISO8601 with a UTC
offset, e.g. "2026-07-15T07:52:52.200000-07:00" -- first 10 chars are the
posting date). No separate lightweight `/_next/data/{buildId}/...json`
endpoint is used even though one exists -- `buildId` is a per-deploy token
that would need its own scrape-and-cache step (AB InBev GCC pattern), and
since a full per-job HTML fetch is already required regardless (the
description isn't available any cheaper way), parsing the embedded JSON
directly from that one HTML response is simpler and avoids a second,
buildId-dependent request per job.
"""
from __future__ import annotations

import html as _html_mod
import json
import re
import time

import requests

_ALGOLIA_APP_ID = "6FNAX3TBEF"
_ALGOLIA_API_KEY = "416caa4690f002ff6fe4a2097623640b"
_ALGOLIA_INDEX = "careers_en-US_production"
_ALGOLIA_URL = f"https://{_ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{_ALGOLIA_INDEX}/query"

_ALGOLIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
    "X-Algolia-API-Key": _ALGOLIA_API_KEY,
    "Referer": "https://www.rippling.com/careers/open-roles",
}

_PAGE_HEADERS = {
    "User-Agent": _ALGOLIA_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
}

_HITS_PER_PAGE = 1000
_MAX_ALGOLIA_PAGES = 20  # defensive cap in case the global board ever exceeds 1000*20 postings

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

_job_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}  # job_id -> (description, posting_date)
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _algolia_query(query: str, page: int, timeout: int) -> dict:
    body = {"query": query, "hitsPerPage": _HITS_PER_PAGE, "page": page}
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(_ALGOLIA_URL, headers=_ALGOLIA_HEADERS, json=body, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Rippling Algolia: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rippling Algolia query failed: {exc}") from exc
    raise RateLimitError(f"Rippling Algolia query: no response -- {last_exc}")


def _is_india_hit(hit: dict) -> bool:
    return any(
        (loc.get("countryCode") or "").upper() == "IN"
        for loc in hit.get("locations") or []
    )


def _india_location_str(hit: dict) -> str:
    names = [
        (loc.get("name") or "").strip()
        for loc in hit.get("locations") or []
        if (loc.get("countryCode") or "").upper() == "IN" and loc.get("name")
    ]
    return "; ".join(dict.fromkeys(names)) or "India"


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Rippling global board once (paginating past Algolia's
    1000-hits-per-page cap if it ever grows that large) and cache every
    India-located job, deduplicated by jobId (a job with multiple India
    office options appears once per location as a separate Algolia record).

    _cache_filled is set before the first request so a transient failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() call
    within the same process (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    by_job: dict[str, dict] = {}
    total_global = 0
    for page in range(_MAX_ALGOLIA_PAGES):
        data = _algolia_query("", page, timeout)
        hits = data.get("hits") or []
        if page == 0:
            total_global = data.get("nbHits", len(hits))
        if not hits:
            break

        for hit in hits:
            if not _is_india_hit(hit):
                continue
            job_id = str(hit.get("jobId") or "").strip()
            title = (hit.get("name") or "").strip()
            url = (hit.get("url") or "").strip()
            if not (job_id and title and url):
                continue
            if job_id in by_job:
                continue
            by_job[job_id] = {
                "id": job_id,
                "title": title,
                "location": _india_location_str(hit),
                "posting_date": "",  # filled lazily by fetch_job_description
                "application_url": url,
            }

        if len(hits) < _HITS_PER_PAGE:
            break

    _job_cache[:] = list(by_job.values())
    print(f"[Rippling] Cache filled: {len(_job_cache)} India jobs (of {total_global} total global postings)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Rippling India postings from the cached global pool.

    keyword/location are accepted for interface compatibility but ignored --
    Algolia's own relevance search is not used for India detection (see
    module docstring re: the Indianapolis false positive); the whole board
    is fetched once, filtered by structured countryCode, cached, and sliced
    here. The shared matcher does the real title/skill filtering.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start: start + num]


def _job_id_from_url(application_url: str) -> str:
    path = (application_url or "").split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Rippling job.

    Fetches the job's own detail-page HTML (no auth/redirect needed) and
    parses the embedded Next.js __NEXT_DATA__ JSON for the description
    fragments and createdOn timestamp -- see module docstring.
    """
    job_id = _job_id_from_url(application_url)
    if job_id in _desc_cache:
        return _desc_cache[job_id]

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_PAGE_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Rippling description: 429 rate-limited for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rippling description fetch failed for {job_id}: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Rippling description fetch: no response for {job_id} -- {last_exc}")

    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        result = ("", "")
        _desc_cache[job_id] = result
        return result

    try:
        next_data = json.loads(m.group(1))
        job_post = next_data["props"]["pageProps"]["apiData"]["jobPost"]
    except (ValueError, KeyError, TypeError):
        result = ("", "")
        _desc_cache[job_id] = result
        return result

    desc_obj = job_post.get("description") or {}
    parts = [desc_obj.get("company") or "", desc_obj.get("role") or ""]
    description = " ".join(_strip_html(p) for p in parts if p)

    posting_date = (job_post.get("createdOn") or "")[:10]

    result = (description, posting_date)
    _desc_cache[job_id] = result
    return result
