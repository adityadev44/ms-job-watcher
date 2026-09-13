"""American Airlines "AA Tech Hub India" job fetcher -- Talent500 marketplace.

ATS identification (Step 1, verified live 2026-09-13 via real DevTools-style
probing, not a vibe): American's own corporate ATS at jobs.aa.com is SAP
SuccessFactors "Job2Web Unify" -- confirmed via `j2w.searchResultsUnify.min.js`
in the page's script list, `companyId: 'americairP'`, and the same
`/services/recruiting/v1/jobs` POST shape already documented for Standard
Chartered/Wipro/HCLTech in PLAYBOOK.md. That tenant's ENTIRE public job board
is tiny (99 jobs total worldwide, confirmed by paging every result) and its
`jobLocationCountry` facet has ZERO India postings -- American's own direct
ATS is not the real India channel.

The real India tech pipeline is a separate, dedicated microsite: "AA Tech Hub
India" (talent500.com/aatechhubindia), a marketing/landing page pointing at
`https://talent500.com/jobs/aatechhubindia/` -- a job board hosted on
Talent500, a third-party GCC/tech-hiring marketplace (same vendor family
already seen powering ANSR-style GCC recruiting pages). This board is 100%
Hyderabad, India and has real, current, high-volume software engineering
demand -- 65 open jobs at investigation time, spanning "Engineer, IT
Software - Java", "... - Fullstack" (Angular/React/Azure/AWS/.Net), "...
Machine Learning", "Manager, IT Data" (Python/PySpark), etc.

Talent500's underlying API (found by pulling apart the React SPA's webpack
chunks at `talent500.com/wp-content/plugins/t500-apply/wp-build/*.js` --
`companyJobsPage.*.js` builds the search request, the base URL constant
resolves to `prod-warmachine.talent500.co/api`):

  - Search (plain GET, no auth, no CSRF):
      GET https://prod-warmachine.talent500.co/api/v3/jobs/search/
          ?company_slug=aatechhubindia&search_term=<kw>&experience_range=
          &offset=<n>&size=<n>
    NOTE: the sibling `v5/jobs/search/` endpoint referenced elsewhere in the
    same JS bundle (`jobSearchV5`) silently ignores `company_slug` and
    returns an unrelated small fallback pool (a GCC-consulting company,
    "ANSR") for ANY unrecognized value including real slugs -- a real
    "looks like it should work" trap. `v3/jobs/search/` (the `jobSearch`
    key, not `jobSearchV5`) is the one actually used by the company-scoped
    jobs page and the only one that returns AA Tech Hub India's real jobs.
    `search_term` does genuinely narrow server-side (confirmed: "engineer"
    -> 38 of 65; a nonsense token -> 0) and `size` was empirically confirmed
    to accept values well above the frontend's own default page size of 7
    (size=100 returned all 65 in one call) -- this fetcher still caches the
    full pool once per process (cheap at this size) and ignores `keyword`/
    `location` from the caller, same "small board, cache once" discipline as
    Lufthansa/Boeing, rather than depending on the caller's specific keyword
    list to enumerate every job.

  - Detail (plain GET, no auth): `GET /api/jobs/{id}/` (note: NOT
    `/api/v3/jobs/{id}/` -- that 404s; the unversioned path is correct) --
    returns a large JSON object including a full HTML `description` field.
    The friendly, human-clickable page
    (`https://talent500.com/jobs/aatechhubindia/{slug}/`) renders this same
    content, but only via client-side React after JS executes (confirmed:
    a plain `requests.get` on that URL returns the SPA shell with no job
    text at all) -- so the fetcher calls the JSON detail API directly
    instead of needing Playwright, using the job `id` (a UUID) cached
    in-module from the search response, keyed by the friendly
    `application_url` this fetcher also builds and returns for the alert.

Location: the search response's `location` field is a bare city name
("Hyderabad") with no country word; `country.name` ("India") is a separate,
reliable field on every posting observed -- combined into "Hyderabad, India"
for `is_india_job()`. Every one of the 65 postings sampled is Hyderabad --
no other India city, no non-India leakage to guard against here.

Posting date: `created_at` is a full ISO-8601 timestamp with a stable date
component (spot-checked against the frontend's own "N days ago" display --
consistent, not a synthetic "today" value like United Airlines' JSON-LD
`datePosted` bug) -- truncated to `YYYY-MM-DD` and used directly.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_API_BASE = "https://prod-warmachine.talent500.co/api"
_SEARCH_URL = f"{_API_BASE}/v3/jobs/search/"
_DETAIL_URL = f"{_API_BASE}/jobs"
_COMPANY_SLUG = "aatechhubindia"
_PUBLIC_BASE = f"https://talent500.com/jobs/{_COMPANY_SLUG}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_cache: list[dict[str, str]] = []
_cache_filled = False
_url_to_id: dict[str, str] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from Talent500's API."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _get(url: str, params: dict | None, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"American Airlines: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"American Airlines fetch failed for {url}: {exc}"
            ) from exc
    raise RateLimitError(f"American Airlines fetch failed for {url}: {last_exc}")


def _fill_cache(timeout: int) -> None:
    r = _get(
        _SEARCH_URL,
        {
            "company_slug": _COMPANY_SLUG,
            "experience_range": "",
            "search_term": "",
            "offset": 0,
            "size": 100,
        },
        timeout,
    )
    data = r.json()
    for j in data.get("data", []) or []:
        jid = j.get("id") or ""
        slug = j.get("slug") or ""
        title = (j.get("title") or j.get("title_alias_1") or "").strip()
        if not jid or not title:
            continue
        if j.get("is_active") is False:
            # Confirmed live: the search API can list a job with
            # status="open" even though is_active is False -- its detail
            # endpoint then returns HTTP 204 with no body (a real posting
            # that closed but wasn't pruned from the search index yet).
            # Skip it here rather than caching a dead detail-fetch target.
            continue
        city = (j.get("location") or "").strip()
        country = ((j.get("country") or {}).get("name") or "").strip()
        if city and country:
            loc = f"{city}, {country}"
        elif country:
            loc = country
        else:
            loc = city or "India"
        posting_date = (j.get("created_at") or "")[:10]
        app_url = f"{_PUBLIC_BASE}/{slug}/" if slug else f"{_PUBLIC_BASE}/{jid}/"
        _url_to_id[app_url] = jid
        _cache.append({
            "id": jid,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return AA Tech Hub India job listings (cached once per process).

    `search_term` genuinely narrows server-side on this API (see module
    docstring), but the full India-only pool is small (~65 jobs) -- this
    fetcher caches it once via a single unfiltered call and ignores the
    caller's `keyword`/`location`, same discipline as Lufthansa/Boeing.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a job's full description + posting date from Talent500's detail API.

    The public job page only renders content client-side via React -- a
    plain HTTP GET on it returns an empty SPA shell -- so this calls the
    unversioned `/api/jobs/{id}/` JSON endpoint directly instead, using the
    job's UUID cached in-module (keyed by `application_url`) from the
    search response that produced this job.
    """
    jid = _url_to_id.get(application_url)
    if not jid:
        return "", ""

    r = _get(f"{_DETAIL_URL}/{jid}/", None, timeout)
    if r.status_code == 204 or not r.text.strip():
        # Job closed between cache fill and this call -- Talent500 returns
        # HTTP 204 with no body for a since-deactivated job ID (see
        # module/_fill_cache docstring). Treat as an empty/unavailable
        # description; matcher.py drops jobs with no fetched description
        # rather than crashing on a JSON parse of an empty body.
        return "", ""
    data = r.json()
    description = _strip_html(data.get("description") or "")
    posting_date = (data.get("created_at") or "")[:10]
    return description, posting_date
