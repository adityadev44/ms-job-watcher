"""
PepsiCo India GCC job fetcher — iCIMS REST API (pepsicojobs.com).

PepsiCo's careers site (www.pepsicojobs.com) *looks* like a bespoke, custom
site at a glance (its own domain, its own branding, "Jibe" CDN assets), but
it is actually built on iCIMS' Jibe Careers Site Builder front end
(app.jibecdn.com / assets.jibecdn.com) sitting on top of the iCIMS ATS
itself — confirmed directly from the search API response's own
``"ats_code": "icims"`` field and ``apply_url`` values pointing at
``*.icims.com``, not assumed from the site's custom-looking branding. This
is the same underlying REST API family already used in this repo by
spglobal_careers_fetcher.py and gallagher_fetcher.py (``GET /api/jobs``,
no auth required).

Full job descriptions — plus separate ``qualifications`` and
``responsibilities`` fields, which sometimes carry the only mention of a
specific skill term — are embedded directly in the search response, so
``fetch_job_description()`` is served entirely from an in-module cache
built during ``fetch_jobs()``; no per-job detail HTTP call is needed.

Both ``keywords`` and ``location`` are genuinely applied server-side
(confirmed empirically): different keyword values return different
``totalCount`` numbers and different result sets, and adding
``location=India`` narrows the global ~2,991-job pool to ~245.

Pagination bug (confirmed via direct A/B requests): the ``offset`` query
parameter is silently ignored — every request, no matter what offset is
passed (0, 5, 20, 50, 100, 200, ...), returns the identical result set
starting from position 0. ``limit`` is hard-capped at 100 — any value above
100 still returns HTTP 200 but with a generic
``{"error": "An unexpected error occurred"}`` body instead of job data.
Worked around the same way this repo already handles broken pagination
elsewhere (UBS/Deutsche Bank/Persistent's "cache once, slice locally"
pattern): the first ``fetch_jobs()`` call for a given keyword hits the real
API once with ``limit=100&offset=0`` and caches the parsed results; every
later call for that same keyword slices the cached list locally instead of
re-hitting the broken remote pagination.

PepsiCo's Hyderabad GCC (cloud/data/AI engineering hub) uses heavy internal
leveling titles ("Deputy Director -", "Senior Manager -", "Associate/Assoc
Principal Engineer", "Architect -") that trip the shared
``matching.exclude_terms``/``title_family`` rules in config.yaml even for
apparent individual-contributor AI/engineering roles — the same class of
false-negative already flagged for BlackRock/Moody's in PLAYBOOK.md. Not
something this fetcher can or should work around; see the onboarding report
for specifics (real LangChain/LangGraph/vector-database description hits
exist today but are currently excluded before reaching the skill check).
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://www.pepsicojobs.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/main",
}

_MAX_PAGE_SIZE = 100  # server hard-caps `limit`; >100 returns a generic error body

# keyword -> parsed India job dicts (up to 100 results — everything the
# server returns for that keyword, since `offset` is ignored; see module
# docstring). Populated lazily, once per distinct keyword value.
_keyword_cache: dict[str, list[dict]] = {}
# application_url -> (description, posting_date), populated alongside _keyword_cache.
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-08-19T06:43:00+0000' -> '2026-08-19'."""
    return raw[:10] if raw else ""


def _location_str(j: dict) -> str:
    """Build the location string, preferring the combined multi-location field.

    ``full_location`` (e.g. "Kraków, Poland; Hyderabad, India") includes every
    site a multi-location posting is open in, so a job hireable in Hyderabad
    but primarily listed elsewhere still contains "India" — required for
    matcher.py's substring is_india_job() check to see it. ``short_location``
    only shows the primary site and would silently drop those postings.
    """
    loc = (j.get("full_location") or j.get("short_location") or "").strip()
    if loc:
        return loc
    city = (j.get("city") or "").strip()
    country = (j.get("country") or "").strip()
    if city or country:
        return f"{city}, {country}".strip(", ")
    return ""


def _search(keyword: str, location: str, timeout: int) -> list[dict]:
    """Hit the real API once for *keyword* and return parsed India job dicts.

    Always requests offset=0 — the server ignores offset anyway (see module
    docstring), so there is no benefit to varying it here.
    """
    params = {
        "keywords": keyword,
        "location": location or "India",
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
                raise RateLimitError(
                    f"PepsiCo search failed after 3 attempts (keyword={keyword!r}): {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    raw_jobs = r.json().get("jobs", [])
    jobs: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        loc = _location_str(j)
        posting_date = _parse_date(j.get("posted_date", ""))
        apply_url = j.get("apply_url") or f"{_BASE_URL}/jobs/{job_id}"

        # Concatenate every text field the API offers — some skill terms
        # (e.g. specific tools named only in "qualifications") never appear
        # in "description" alone.
        raw_text = " ".join(
            part
            for part in (
                j.get("description", ""),
                j.get("qualifications", ""),
                j.get("responsibilities", ""),
            )
            if part
        )
        description = _strip_html(raw_text)
        _desc_cache[apply_url] = (description, posting_date)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": apply_url,
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
    """Return a slice of PepsiCo's India job results for *keyword*.

    The server ignores `offset` entirely (see module docstring), so the
    first call for a given keyword fetches and caches up to 100 India
    results in one request; every later call for that same keyword slices
    the cached list locally instead of re-querying the broken remote
    pagination. Caching is keyed only by keyword (not location) because
    production config always searches a single fixed location ("India").
    """
    key = keyword or ""
    if key not in _keyword_cache:
        _keyword_cache[key] = _search(key, location, timeout)
    return _keyword_cache[key][start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) — served from the cache fetch_jobs() built.

    Falls back to a live HTML fetch only if the cache is somehow missing the
    URL (should not normally happen, since every job returned by fetch_jobs()
    populates this cache first).
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
