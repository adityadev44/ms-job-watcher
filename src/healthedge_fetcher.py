"""
HealthEdge job fetcher — iCIMS REST API via Jibe Careers Site Builder
(careers.healthedge.com).

HealthEdge (US healthcare-payer/insurance software company: HealthRules
Payer, GuidingCare, Source, Wellframe) looked like it could be running a
bespoke/custom careers site at a glance (its own domain, its own branding),
but a real DevTools-style check (view-source + a direct hit on the search
endpoint) shows the same "Jibe" front-end CDN assets (``app.jibecdn.com`` /
``assets.jibecdn.com``) already seen at PepsiCo/Schneider Electric, and the
search API response's own ``"ats_code": "icims"`` field plus every
``apply_url`` pointing at ``*.icims.com`` (two tenants observed:
``careers-healthedge.icims.com`` and ``wellframe-healthedge.icims.com`` —
HealthEdge's Wellframe subsidiary has its own iCIMS site code but shares the
same public search API) confirms it. Same underlying REST family as
``pepsico_fetcher.py``, ``schneiderelectric_fetcher.py``, ``gallagher_fetcher.py``,
and ``spglobal_careers_fetcher.py`` (``GET /api/jobs``, no auth required).

Verified via direct A/B requests against the live API (not assumed):
- ``location=India`` genuinely filters server-side: 34 of 80 global postings,
  matching (with one edge case — see below) a manual substring check of the
  unfiltered 80-job pool's own ``full_location``/``country`` fields.
- ``keywords`` genuinely narrows server-side too (e.g. ``keywords=Engineer``
  returns ``totalCount: 44`` vs. 80 unfiltered) — but this fetcher
  deliberately ignores it and always fetches the *full* India pool in one
  shot instead (see below), same choice already made for MetLife/Infosys
  ("empty keyword fetches everything; title/skill filters handle the rest").
  Registered in ``_IGNORES_KEYWORDS`` for this reason.
- ``offset`` is silently ignored (identical results at offset=0 and
  offset=5, the same broken-pagination shape as PepsiCo's tenant on this
  same ATS family) — irrelevant here anyway since the whole India pool (34
  jobs) fits comfortably under the single-page ``limit`` cap.
- ``limit`` hard-caps at 100 — a request above that returns HTTP 422 (like
  Schneider Electric's tenant, not PepsiCo's "200 with a generic error body"
  variant). 100 comfortably covers today's 34-job India pool; if the India
  count ever exceeds 100 some postings would silently go unseen with no
  error surfaced — a known, documented limitation shared with every other
  cache-once fetcher in this repo, not fixed defensively here.

Full job description is embedded directly in the search response as a
single HTML ``description`` field (no separate ``qualifications``/
``responsibilities`` fields on this tenant, unlike PepsiCo/Schneider
Electric's schema) — ``fetch_job_description()`` is served entirely from an
in-module cache built during ``fetch_jobs()``.

``apply_url`` (``*.icims.com/jobs/{id}/login``) sits behind an AWS WAF
bot-challenge for plain HTTP traffic — confirmed via direct probe: HTTP 405
with a literal "Human Verification"/CAPTCHA body, the same protection class
already documented for Schneider Electric/IBM's job-detail pages. Rather
than reach for Playwright (unnecessary since the description is already
inline), alerts link to the public, unauthenticated
``careers.healthedge.com/jobs/{id}?lang=en-us`` page instead, which was
confirmed via direct fetch to render the correct job (matching `<title>`)
with no login wall.

Live-verified 2026-09-04: 34 India postings (Bangalore, Hyderabad,
Coimbatore, Kochi, Trivandrum — Coimbatore is Tamil Nadu but the location
text never says so, same "excluded by name" pattern as State Street's
tenant; Kochi/Trivandrum are already covered by the shared
`exclude_locations` defaults). Real, current `.NET / C#` matches exist
today: several "Software Engineer (.Net Full-Stack)" / "Technical Lead
(.Net Fullstack)" titles in Bangalore/Hyderabad explicitly name C#, ASP.NET,
SQL Server, Entity Framework, and Web API in the JD body — see the
onboarding report for exact job IDs.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://careers.healthedge.com"
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
# Schneider Electric's tenant) -- comfortably above today's 34-job India pool.
_MAX_PAGE_SIZE = 100

# Populated once by the first fetch_jobs() call; every later call (any
# keyword -- keywords are ignored, see module docstring) slices this list
# locally instead of re-querying. Same cache-once shape as
# hexaware_fetcher.py / persistent_fetcher.py.
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
    """'2026-09-04T01:58:00+0000' -> '2026-09-04'."""
    return raw[:10] if raw else ""


def _location_str(j: dict) -> str:
    """Prefer the combined multi-location field, same reasoning as the
    PepsiCo/Schneider Electric fetchers on this same ATS family: a job
    hireable in India but not primarily listed there could otherwise lose
    the "India" substring matcher.py's is_india_job() needs.
    """
    loc = (j.get("full_location") or j.get("short_location") or "").strip()
    if loc:
        return loc
    city = (j.get("location_name") or "").strip()
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
                raise RateLimitError(
                    f"HealthEdge search failed after 3 attempts: {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    try:
        raw_jobs = r.json().get("jobs", [])
    except ValueError as exc:
        raise RateLimitError(f"HealthEdge search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        loc = _location_str(j)
        posting_date = _parse_date(j.get("posted_date", ""))
        # apply_url (*.icims.com/jobs/{id}/login) is AWS-WAF-gated for plain
        # HTTP (HTTP 405 + CAPTCHA, confirmed by direct probe) -- link to the
        # public, unauthenticated careers.healthedge.com job page instead.
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
    """Return a slice of HealthEdge's India job results.

    `keyword` is accepted but ignored (see module docstring -- registered in
    company_registry._IGNORES_KEYWORDS, so run_company.py only ever calls
    this once per run with a single placeholder keyword). The first call
    fetches and caches the full ~34-job India pool in one request; every
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
