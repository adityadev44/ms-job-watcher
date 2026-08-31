"""Fetches Meta (metacareers.com) job listings via Meta's internal GraphQL API.

metacareers.com is a client-side "Comet"/Relay React app — the initial HTML
has no job data embedded, and every result comes from `POST /graphql` calls
using Facebook's internal (undocumented) GraphQL protocol. Despite that, this
turned out to be fully usable with plain, unauthenticated `requests`:

- A single `GET /jobs` (with browser-like `Sec-Fetch-*` headers — a bare
  `curl`/`requests` GET with no headers gets a generic HTTP 400 "something
  went wrong" page; `Sec-Fetch-Mode: navigate` etc. fixes this) returns HTML
  containing an anonymous `LSD` token (`"LSD",[],{"token":"..."}`) plus a
  `datr` tracking cookie — no login, no session state, no CSRF handshake
  beyond this.
- `jazoest` (FB's checksum companion to `lsd`) is derivable with the
  well-known public formula: `"2" + sum(ord(c) for c in lsd)`.
- `POST /graphql` with `doc_id=27129360303422352`,
  `fb_api_req_friendly_name=CareersJobSearchResultsV2DataQuery`, and a
  `search_input.offices` list of Meta's own India office-location IDs
  (`bangalore`, `gurgaon`, `hyderabad`, `mumbai`, `newdelhi` — confirmed via
  the site's own `CareersJobSearchLocationFilterV3Query`) returns clean JSON
  with every open India job in one shot — no pagination cap observed
  (`results_per_page: null` returns the whole matching set), zero non-India
  leakage. `__user=0`/`isLoggedIn: false` confirm this is a genuinely
  anonymous, unauthenticated call — no bot-challenge, no CAPTCHA.
- Job-detail pages (`/jobs/{id}/`) are plain server-rendered HTML with a
  clean schema.org `JobPosting` JSON-LD block (`description`,
  `responsibilities`, `qualifications`, `datePosted`) — fetchable with a
  cold, cookie-less `requests.get()`, no prior session needed at all.

Risk to flag for future maintainers: `doc_id` is an internal per-build query
identifier, not a stable public contract — Meta can rotate it on a frontend
deploy (an invalid `doc_id` fails cleanly with HTTP 404, which surfaces here
as a `RateLimitError` after 3 retries, not a silent wrong-data bug). If this
fetcher starts returning 0 jobs, re-capture the doc_id: open
https://www.metacareers.com/jobs in a browser with DevTools Network open,
filter for `graphql`, and read the `fb_api_req_friendly_name`/`doc_id` pair
off the `CareersJobSearchResultsV2DataQuery` request.

As of 2026-08-31, Meta's own India office IDs surface ~20 open India roles
total (Bangalore/Gurgaon/Mumbai) — overwhelmingly ASIC/hardware, marketing,
and sales titles, not Software Engineering. Zero matches through the full
pipeline is a real current fact about Meta's India hiring mix, not a
fetcher defect (same shape as the ING/eClerx zero-match cases already in
the registry).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_BASE = "https://www.metacareers.com"
_JOBS_PAGE = f"{_BASE}/jobs"
_GRAPHQL_URL = f"{_BASE}/graphql"

_DOC_ID = "27129360303422352"
_FRIENDLY_NAME = "CareersJobSearchResultsV2DataQuery"

# Meta's own office-location IDs for its India sites (from
# CareersJobSearchLocationFilterV3Query). Filtering server-side on these
# means zero client-side "is this really India" guessing is needed.
_INDIA_OFFICES = ["bangalore", "gurgaon", "hyderabad", "mumbai", "newdelhi"]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Plain navigation-style headers. Required: without Sec-Fetch-Mode/-Site the
# site's edge returns a generic HTTP 400 error page to every GET.
_HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}

_LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)

# Module-level cache: India job list is tiny (~20 jobs) and Meta's own search
# ignores query keywords in our usage (we filter server-side by office only,
# same "fetch once, let matcher filter" pattern as several other companies).
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429, a 404'd/rotated doc_id, or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert ISO-8601 'YYYY-MM-DDTHH:MM:SS±HH:MM' -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    return raw.split("T", 1)[0]


def _get_with_retries(url: str, timeout: int, session: requests.Session | None = None):
    get = session.get if session is not None else requests.get
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = get(url, headers=_HTML_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Meta: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Meta fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Meta fetch: no response for {url} — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before attempting — avoid a per-keyword retry storm

    session = requests.Session()
    r = _get_with_retries(_JOBS_PAGE, timeout, session)

    m = _LSD_RE.search(r.text)
    if not m:
        raise RateLimitError(
            "Meta: could not find LSD token in /jobs HTML — page structure may have changed"
        )
    lsd = m.group(1)
    jazoest = "2" + str(sum(ord(c) for c in lsd))

    variables = {
        "search_input": {
            "q": None,
            "divisions": [],
            "offices": _INDIA_OFFICES,
            "roles": [],
            "leadership_levels": [],
            "saved_jobs": [],
            "saved_searches": [],
            "sub_teams": [],
            "teams": [],
            "is_leadership": False,
            "is_remote_only": False,
            "sort_by_new": False,
            "results_per_page": None,
        },
        "viewasUserID": None,
        "isLoggedIn": False,
    }
    payload = {
        "av": "0",
        "__user": "0",
        "__a": "1",
        "__req": "1",
        "dpr": "2",
        "lsd": lsd,
        "jazoest": jazoest,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": _FRIENDLY_NAME,
        "server_timestamps": "true",
        "variables": json.dumps(variables),
        "doc_id": _DOC_ID,
    }
    post_headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": _BASE,
        "Referer": _JOBS_PAGE,
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Dest": "empty",
        "X-FB-Friendly-Name": _FRIENDLY_NAME,
    }

    last_exc: Exception | None = None
    resp = None
    for attempt in range(3):
        try:
            resp = session.post(
                _GRAPHQL_URL, data=payload, headers=post_headers, timeout=timeout
            )
            if resp.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Meta: 429 rate-limited on /graphql")
            resp.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Meta /graphql call failed after 3 attempts (doc_id may have "
                f"rotated — see module docstring): {exc}"
            ) from exc

    if resp is None:
        raise RateLimitError(f"Meta: no /graphql response — {last_exc}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise RateLimitError(f"Meta /graphql: non-JSON response: {exc}") from exc

    try:
        all_jobs = data["data"]["job_search_with_featured_jobs_v2"]["all_jobs"]
    except (KeyError, TypeError) as exc:
        raise RateLimitError(
            f"Meta /graphql: unexpected response shape (doc_id may have "
            f"rotated — see module docstring): {data!r}"[:500]
        ) from exc

    collected: list[dict] = []
    for job in all_jobs:
        job_id = str(job.get("id", ""))
        title = job.get("title", "")
        locations = job.get("locations") or []
        if not (job_id and title and locations):
            continue
        location = "; ".join(locations)
        if "india" not in location.lower():
            continue  # defensive; office filter should already guarantee this
        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",  # filled in on description fetch (JSON-LD datePosted)
            "application_url": f"{_BASE}/jobs/{job_id}/",
        })

    _india_cache = collected
    print(f"[Meta] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Meta India jobs.

    Meta's own India office IDs are applied server-side (no client-side
    India-detection heuristics needed); the same cached list is re-served
    for every keyword call — Meta's search endpoint is not queried per
    keyword since the full India pool is tiny (~20 jobs) and cheap to keep
    in memory for the whole run.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) parsed from the detail page's JSON-LD.

    No session/cookie state needed — a cold, unauthenticated GET renders the
    full schema.org JobPosting JSON-LD block server-side.
    """
    r = _get_with_retries(application_url, timeout)

    m = _JSONLD_RE.search(r.text)
    if not m:
        return "", ""

    try:
        data = json.loads(m.group(1))
    except ValueError:
        return "", ""

    parts = [
        data.get("description", "") or "",
        data.get("responsibilities", "") or "",
        data.get("qualifications", "") or "",
    ]
    description = _strip_html(" ".join(p for p in parts if p))
    posting_date = _parse_date(data.get("datePosted", "") or "")
    return description, posting_date
