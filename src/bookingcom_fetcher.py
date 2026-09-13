"""
Booking.com (Booking Holdings) job fetcher — iCIMS REST API via Jibe
Careers Site Builder (jobs.booking.com).

The public careers site (careers.booking.com) is a WordPress-ish marketing
front-end whose "See all jobs" button links out to `jobs.booking.com/booking/
jobs` — a *different* domain hosting the real Jibe-branded search page
(`app.jibecdn.com`/`assets.jibecdn.com` assets, ``jasession``/``rms-node``
cookies, ``ng-app="jibeapply"`` fallback shell). This is the same "Jibe
Careers Site Builder over iCIMS" ATS family already integrated at
PepsiCo/Schneider Electric/HealthEdge/SITA/Gallagher/S&P Global Careers
(``GET /api/jobs``, no auth). Confirmed fresh from a real DevTools-style
check, not a reused guess — every job's own ``ats_code`` field reads
``"icims"`` and every ``apply_url`` points at a
``*-workingatbooking.icims.com`` host.

One wrinkle not seen at the sibling companies: the search UI's own path is
``jobs.booking.com/booking/jobs`` (a ``/booking/`` brand-path prefix — this
tenant is shared across Booking Holdings' portfolio brands, e.g. Agoda/
Priceline/Kayak/OpenTable/Rentalcars all show up in the page's footer), but
the JSON API itself lives at the bare ``jobs.booking.com/api/jobs`` with NO
``/booking/`` prefix — hitting ``/booking/api/jobs`` (the naive guess) 404s.
Individual postings also carry a ``brand`` field ("Booking.com" vs "Booking
Holdings" vs presumably other portfolio brands) but ``location=India``
already scopes correctly across all of them without needing to filter on
brand separately.

Verified via direct A/B requests against the live API (2026-09-13):
- ``location=India`` genuinely filters server-side (32 jobs returned,
  matches the response's own ``totalCount``).
- ``keywords`` also genuinely narrows server-side (``keywords=engineer``:
  ``totalCount`` drops from 32 to 11) — but deliberately ignored here, same
  choice as SITA/HealthEdge/MetLife/Infosys: the whole India pool comfortably
  fits in one request, so the fetcher always pulls everything and lets
  title/skill filters do the rest. Registered in ``_IGNORES_KEYWORDS``.
- ``offset`` is silently ignored (offset=5&limit=5 returns the identical
  first 5 jobs as offset=0) — same broken-pagination shape as PepsiCo/
  HealthEdge/SITA on this ATS family. Irrelevant here since the whole pool
  fits under the page cap anyway.
- ``limit`` hard-caps at 100 (an explicit 250 returns HTTP 422) — comfortably
  covers today's 32-job India pool.

Full job description is embedded directly in the search response as a
single HTML-ish plain-text ``description`` field — ``fetch_job_description()``
is served entirely from an in-module cache built during ``fetch_jobs()``,
same as SITA/HealthEdge.

``apply_url`` (``*-workingatbooking.icims.com/jobs/{id}/login``) returns
HTTP 405 for plain HTTP traffic, same protection class as HealthEdge/SITA/
IBM's job-detail pages. Alerts instead link to the public, unauthenticated
``jobs.booking.com/booking/jobs/{id}`` page — confirmed via direct fetch to
render the correct job title with no login wall (a guessed
``/booking/job/{id}`` singular path 404s; the plural ``/booking/jobs/{id}``
is correct).

Live-verified 2026-09-13: 32 India postings, all Mumbai (1) or Bengaluru
(31) — no Pune/Chennai/Kochi/Tamil Nadu postings observed today. Mix is
mostly Finance/Accounting/Tax/HR roles plus a real engineering slice:
"Senior Full Stack Application Developer" (React/Angular/Node.js/Java/
Python + explicit LLM/Generative AI/RAG requirements — a strong AI/ML
match, though it also requires "12+ years" and will correctly be dropped by
the shared 10+ YOE filter), "Software Engineer 2", "Senior Platform
Engineering - IC - G - ADS", "Senior Platform Engineer I - Evergreen", and
"Data & AI Governance Architect". `require_tech_in_description` deliberately
left off — titles here are specific enough (not generic IT-services level
bands) that Layer 3's skill check alone is a reliable signal.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://jobs.booking.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/booking/jobs",
}

# Server hard-caps `limit` at 100 (a higher value returns HTTP 422, same
# family as SITA/Schneider Electric/HealthEdge) -- comfortably above today's
# 32-job India pool.
_MAX_PAGE_SIZE = 100


class RateLimitError(Exception):
    pass


# Populated once by the first fetch_jobs() call; every later call (any
# keyword -- keywords are ignored, see module docstring) slices this list
# locally instead of re-querying. Same cache-once shape as
# sita_fetcher.py / healthedge_fetcher.py.
_india_jobs_cache: list[dict] | None = None
# application_url -> (description, posting_date), populated alongside the cache.
_desc_cache: dict[str, tuple[str, str]] = {}


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-08-31T14:00:00+0000' -> '2026-08-31'."""
    return raw[:10] if raw else ""


def _location_str(j: dict) -> str:
    """Prefer the combined location field -- already "City, State, India" on
    this tenant, so no client-side ", India" append is needed here (unlike
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
                raise RateLimitError(f"Booking.com search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    try:
        raw_jobs = r.json().get("jobs", [])
    except ValueError as exc:
        raise RateLimitError(f"Booking.com search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        loc = _location_str(j)
        posting_date = _parse_date(j.get("posted_date", ""))
        # apply_url (*-workingatbooking.icims.com/jobs/{id}/login) returns
        # HTTP 405 for plain HTTP traffic -- link to the public,
        # unauthenticated jobs.booking.com job page instead.
        application_url = f"{_BASE_URL}/booking/jobs/{job_id}"

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
    """Return a slice of Booking.com's India job results.

    `keyword` is accepted but ignored (see module docstring -- registered in
    company_registry._IGNORES_KEYWORDS, so run_company.py only ever calls
    this once per run with a single placeholder keyword). The first call
    fetches and caches the full ~32-job India pool in one request; every
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
