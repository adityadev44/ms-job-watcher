"""
ZS Associates job fetcher -- iCIMS REST API via Jibe Careers Site Builder
(jobs.zs.com).

ZS's public careers page (www.zs.com/careers) redirects to a bespoke-looking
`jobs.zs.com` domain, but a direct hit on the search endpoint reveals the
same Jibe front-end (`data-jibe-search-version` marker in the page source,
`app.jibecdn.com` assets) already documented for PepsiCo/Schneider
Electric/HealthEdge/SITA, and every job record's own `"ats_code": "icims"`
field plus `apply_url` pointing at `careers-zs.icims.com` confirms it. Same
`GET /api/jobs` REST family, no auth required.

Verified via direct A/B requests against the live API (not assumed):
- `country=India` genuinely filters server-side: 47 of 257 global postings
  (`country_code=IN` is silently ignored -- returns the full 257 -- use the
  literal country name like HealthEdge's `location=India`, not a country
  code).
- `limit=100` in one shot covers the entire 47-job India pool; no
  pagination needed.
- Full job description is embedded directly in the search response as a
  single HTML `description` field -- `fetch_job_description()` is served
  entirely from an in-module cache built during `fetch_jobs()`.
- `apply_url` (`careers-zs.icims.com/jobs/{id}/login`) returns HTTP 410 to
  plain HTTP (stale/expired session-bound link) -- alerts link to the
  public, unauthenticated `jobs.zs.com/jobs/{id}?lang=en-us` page instead
  (confirmed HTTP 200, renders the correct job).

Live-verified 2026-09-13: 47 India postings, split Pune (35, excluded),
Bengaluru (6), Gurgaon (6). Genuine AI/ML engineering titles exist in the
non-Pune subset: "Business Technology Solutions Associate Consultant - AI
Engineering" (Bengaluru), "Lead AI Scientist" / "Senior Lead AI Scientist"
(Bengaluru), "Engineering Manager (Software Development and DevOps)"
(Bengaluru) -- several explicitly name Python/ML/LLM stacks in the JD body.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://jobs.zs.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/jobs",
}

# Server comfortably returns the whole ~47-job India pool under this cap
# (HealthEdge/Schneider Electric's tenants hard-cap at 100 too).
_MAX_PAGE_SIZE = 100

# Populated once by the first fetch_jobs() call; every later call (any
# keyword -- keywords are ignored, see module docstring) slices this list
# locally instead of re-querying. Same cache-once shape as
# healthedge_fetcher.py / hexaware_fetcher.py.
_india_jobs_cache: list[dict] | None = None
# application_url -> (description, posting_date), populated alongside the cache.
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-09-11T21:19:00+0000' -> '2026-09-11'."""
    return raw[:10] if raw else ""


def _location_str(j: dict) -> str:
    """Prefer the combined multi-location field -- a job hireable in India
    but not primarily listed there could otherwise lose the "India"
    substring matcher.py's is_india_job() needs (same reasoning as the
    PepsiCo/HealthEdge fetchers on this same ATS family).
    """
    loc = (j.get("full_location") or j.get("short_location") or "").strip()
    if loc:
        return loc
    city = (j.get("city") or j.get("location_name") or "").strip()
    country = (j.get("country") or "").strip()
    if city or country:
        return f"{city}, {country}".strip(", ")
    return ""


def _fetch_india_pool(timeout: int) -> list[dict]:
    """Hit the real API once for the full India job pool and return parsed dicts.

    Always requests country=India, limit=100, offset=0 -- keywords are
    deliberately never sent (this fetcher is registered in
    _IGNORES_KEYWORDS).
    """
    params = {
        "country": "India",
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
                raise RateLimitError(f"ZS Associates search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    try:
        raw_jobs = r.json().get("jobs", [])
    except ValueError as exc:
        raise RateLimitError(f"ZS Associates search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        loc = _location_str(j)
        posting_date = _parse_date(j.get("posted_date", ""))
        # apply_url (careers-zs.icims.com/jobs/{id}/login) returned HTTP 410
        # on direct probe -- link to the public, unauthenticated
        # jobs.zs.com job page instead.
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
    """Return a slice of ZS Associates's India job results.

    `keyword` is accepted but ignored (registered in
    company_registry._IGNORES_KEYWORDS, so run_company.py only ever calls
    this once per run with a single placeholder keyword). The first call
    fetches and caches the full ~47-job India pool in one request; every
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
