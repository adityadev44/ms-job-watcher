"""Ferguson Bengaluru GCC job fetcher -- Talent500 marketplace.

ATS identification (Step 1, verified live 2026-09-13): Ferguson's own
corporate careers site (ferguson.wd1.myworkdayjobs.com/Ferguson_Experienced)
is a real Workday tenant, but it is US-only -- probing it directly confirms
`Location_Country` facet only has "United States of America" and a full-text
search for "India"/"Bengaluru" returns zero genuine India postings (the sole
"India" hits were "Indianapolis, IN" substring false positives). Ferguson's
Bengaluru Global Capability Center, launched June 2025 in partnership with
ANSR (see press coverage: "Ferguson Launches Global Capability Center Powered
by ANSR in Bengaluru"), posts its own openings on a separate, dedicated
board on **Talent500** -- the same third-party GCC/tech-hiring marketplace
already integrated in this repo for American Airlines' "AA Tech Hub India"
(see `americanairlines_fetcher.py`). Ferguson's company slug on Talent500 is
simply `ferguson` (confirmed live: `talent500.com/jobs/ferguson/...` URLs,
`company.slug == "ferguson"` in every search hit).

API shape is identical to American Airlines' Talent500 integration (same
vendor, same endpoints) -- see that fetcher's docstring for the full
discovery story of the `v3` vs `v5` search-endpoint trap and the `/api/jobs/
{id}/` detail shape. Summary:

  - Search (plain GET, no auth): `GET https://prod-warmachine.talent500.co
    /api/v3/jobs/search/?company_slug=ferguson&search_term=&offset=0&size=100`
    -- confirmed live: 13 total jobs, ALL genuinely Bengaluru/India
    (`country.name == "India"` on every hit), spanning real software
    engineering roles (Lead Cloud Engineer - Azure, Senior QA Engineer,
    Lead Business Analyst - Order Management, etc.) -- a small but real and
    current board, same "onboard pre-emptively" precedent as Zurich
    Insurance elsewhere in this repo, except Ferguson's board already has
    live postings today, not zero.
  - Detail (plain GET, no auth): `GET /api/jobs/{id}/` -- full HTML
    `description` field, same shape as AA Tech Hub India.

Location: every observed posting's `location` field is "Bengaluru" with
`country.name == "India"` as a separate field -- combined into "Bengaluru,
India" for `is_india_job()`. No non-Bengaluru India city seen in this small
pool, and no non-India leakage at all (all 13 confirmed India).

Posting date: `created_at` is a full ISO-8601 timestamp, truncated to
`YYYY-MM-DD`.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_API_BASE = "https://prod-warmachine.talent500.co/api"
_SEARCH_URL = f"{_API_BASE}/v3/jobs/search/"
_DETAIL_URL = f"{_API_BASE}/jobs"
_COMPANY_SLUG = "ferguson"
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
                raise RateLimitError(f"Ferguson: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Ferguson fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Ferguson fetch failed for {url}: {last_exc}")


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
            # Same "listed but deactivated" edge case documented for AA
            # Tech Hub India -- skip rather than cache a dead detail target.
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
    """Return Ferguson Bengaluru GCC job listings (cached once per process).

    The full India-only pool is tiny (13 jobs at onboarding time) -- this
    fetcher caches it once via a single unfiltered call and ignores the
    caller's `keyword`/`location`, same discipline as AA Tech Hub India.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a job's full description + posting date from Talent500's detail API.

    Same shape as AA Tech Hub India: the public page only renders content
    client-side via React, so this calls the unversioned `/api/jobs/{id}/`
    JSON endpoint directly using the UUID cached in-module during search.
    """
    jid = _url_to_id.get(application_url)
    if not jid:
        return "", ""

    r = _get(f"{_DETAIL_URL}/{jid}/", None, timeout)
    if r.status_code == 204 or not r.text.strip():
        return "", ""
    data = r.json()
    description = _strip_html(data.get("description") or "")
    posting_date = (data.get("created_at") or "")[:10]
    return description, posting_date
