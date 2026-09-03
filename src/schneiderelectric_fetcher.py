"""
Schneider Electric job fetcher — iCIMS REST API via Jibe Careers Site Builder
(careers.se.com).

Prior secondary research had labeled this company's ATS "custom" — wrong,
per the same lesson already recorded in PLAYBOOK.md's Wave 5 entry (Boeing/
PepsiCo/Walmart/United Airlines all had the identical wrong "custom" guess).
Verified from scratch via a real DevTools-style network check: the page
loads ``app.jibecdn.com``/``assets.jibecdn.com`` (iCIMS' "Jibe" front-end
builder — same product PepsiCo's ``pepsicojobs.com`` uses) and the search
API response's own ``"ats_code": "icims"`` field, with every ``apply_url``
pointing at ``careers-se.icims.com``, confirms the real ATS behind the
branded domain. Same underlying REST family as ``pepsico_fetcher.py``,
``gallagher_fetcher.py``, and ``spglobal_careers_fetcher.py``
(``GET /api/jobs``, no auth required).

Unlike PepsiCo's tenant, this tenant's pagination is NOT broken: ``offset``
and ``limit`` were verified via direct A/B requests to genuinely page
through results (zero ID overlap between offset=0 and offset=5 pages; an
offset past the true total returns an empty list with no wraparound to
page 1, unlike the UBS/MUFG/Nvidia/Pfizer/Walmart pagination-wraparound
family in PLAYBOOK.md). ``keywords`` and ``location`` both genuinely filter
server-side too (confirmed via differing ``totalCount`` values per keyword,
and location=India narrowing the global pool). Because of this, no
cache-once-per-keyword workaround is needed here — every ``fetch_jobs()``
call proxies straight through to the real API.

``limit`` hard-caps at 100 (like PepsiCo/Gallagher's tenants) — any higher
value returns HTTP 422, not a clean error body — so requests are defensively
clamped before sending, same discipline as the Northern Trust/Shell/Nvidia
over-cap lessons in PLAYBOOK.md.

Full job text (``description`` + ``qualifications`` + ``responsibilities``)
is embedded directly in the search response and is already plain text (no
HTML markup observed across a 533-job sample) — ``fetch_job_description()``
is served entirely from an in-module cache built during ``fetch_jobs()``; a
light HTML-strip is kept anyway as defense in depth.

Posting date uses the ``create_date`` field (clean ISO-8601, e.g.
``"2026-08-31T07:16:30+0000"``), NOT the ``posted_date`` field this tenant
returns as a human-formatted string (``"August 31, 2026"``) — slicing that
with ``raw[:10]`` (the pattern ``gallagher_fetcher.py``/``pepsico_fetcher.py``
use on their own tenants' ``posted_date``) would silently produce garbage
like ``"August 31,"``. Confirmed by direct probing that this is a genuine
cross-tenant inconsistency in the same iCIMS/Jibe field name, not a copy-paste
bug in those other fetchers: Gallagher's and PepsiCo's own tenants really do
return ``posted_date`` as ISO-8601 already. Cross-checked several Schneider
jobs and ``create_date`` always matches the same calendar day as the
human-formatted ``posted_date``, so this is a safe substitute.

Live-verified 2026-09-03: 533 total India postings. 42 titles pass the
shared ``title_family``/``exclude_terms`` check, but ZERO currently pass
either ``primary_skills`` group (no ".NET"/"C#"/"ASP.NET"/"Entity Framework"
and no LangChain/RAG/vector-DB hard AI terms) — this is a genuine current
fact, not a fetcher defect (same "confirmed-zero" class as ING/eClerx/AIG/
Boeing/PepsiCo/Pfizer already documented in PLAYBOOK.md). Schneider's
Bengaluru/Hyderabad/Gurgaon software-engineering postings run Java/Spring/
Angular/Azure/embedded-IoT stacks, not .NET or generative-AI/RAG work, as
of this snapshot. Most of the 42 title-passing jobs are generic
"Senior Engineer I/II" level bands spanning many non-software disciplines
(Electromechanics, Cybersecurity Testing, Supply Chain Performance,
Materials Engineering, Technical Customer Care, ...) that only pass because
bare "senior engineer" is in the shared ``title_family`` list — the same
IT-services-style generic-title pattern PLAYBOOK.md's Layer 4
(``require_tech_in_description``) exists for. NOT enabled here: Layer 3's
``primary_skills`` hard-term gate (already narrowed 2026-08-31 to drop
SQL Server/EF/Web API from the .NET group) already reduces the 42
title-passing jobs to zero real matches today, so Layer 4 would not change
current behavior at all — it would only pre-emptively guard against a
future false positive that hasn't been observed. See the onboarding report
for the full reasoning.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://careers.se.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/jobs",
}

# Server hard-caps `limit`; a request above this returns HTTP 422 (confirmed
# via direct probing: 100 -> HTTP 200, 150 -> HTTP 422). matcher.py only ever
# requests num=20 per page, but clamp defensively anyway (Northern Trust /
# Shell / Nvidia over-cap lesson in PLAYBOOK.md).
_MAX_PAGE_SIZE = 100

# application_url -> (description, posting_date), populated by fetch_jobs().
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-08-31T07:16:30+0000' -> '2026-08-31'."""
    return raw[:10] if raw else ""


def _location_str(j: dict) -> str:
    """Prefer the combined multi-location field.

    ``full_location`` (e.g. "Barcelona, Spain; Bangalore, India") lists every
    site a multi-location posting is open in, so a job whose primary country
    is elsewhere but is also hireable in Bangalore still contains "India" —
    required for matcher.py's substring is_india_job() check. ``short_location``
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


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return one page of Schneider Electric's job search results.

    Both `keywords` and `location` are genuinely applied server-side, and
    `offset`/`limit` paginate cleanly with no wraparound (see module
    docstring) — every call proxies straight through to the real API, no
    local caching of the full pool is needed.
    """
    params = {
        "keywords": keyword,
        "location": location or "India",
        "limit": min(num, _MAX_PAGE_SIZE) if num else _MAX_PAGE_SIZE,
        "offset": start,
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
                    f"Schneider Electric search failed after 3 attempts "
                    f"(keyword={keyword!r}): {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    try:
        raw_jobs = r.json().get("jobs", [])
    except ValueError as exc:
        raise RateLimitError(f"Schneider Electric search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        loc = _location_str(j)
        posting_date = _parse_date(j.get("create_date", ""))
        apply_url = j.get("apply_url") or f"{_BASE_URL}/jobs/{job_id}"

        # Concatenate every text field the API offers — some skill terms only
        # appear in "qualifications" or "responsibilities", not "description".
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


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) — served from the cache fetch_jobs() built.

    A live fallback fetch of `application_url` is not useful here: it points
    at careers-se.icims.com, which sits behind AWS WAF bot-challenge/CAPTCHA
    protection for plain `requests` traffic (confirmed: returns HTTP 405
    "Human Verification" with no job content) — the same class of protection
    IBM's job-detail pages have (see PLAYBOOK.md), except here there's no
    need to reach for Playwright since the full description is already
    available inline from the search API and cached during fetch_jobs().
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]
    return "", ""
