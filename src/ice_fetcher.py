"""
ICE (Intercontinental Exchange) job fetcher — iCIMS/Jibe career-site REST API.

careers.ice.com is an iCIMS-owned "Jibe" career-site front end (confirmed via
the `jrasession`/`jasession` cookies and `app.jibecdn.com`/`assets.jibecdn.com`
script bundles served on first load, plus 22 literal "icims"/"iCIMS" string
occurrences in the page HTML — Jibe was acquired by iCIMS in 2019 and is used
as the search widget on top of the iCIMS ATS backend). The page itself is a
client-rendered SPA, but it calls a clean first-party JSON endpoint directly:

    GET https://careers.ice.com/api/jobs?location=India&limit=100

Same "iCIMS REST API, full description inline" shape as spglobal_careers and
gallagher elsewhere in this repo, but with two quirks neither of those hit:

1. `limit` is honored (fails with HTTP 422 above ~100-119; 100 is safe and
   comfortably covers the current India pool) but `offset`/`keywords` are
   NOT — every page request returns the identical top-N jobs regardless of
   offset, and `keywords` narrows results yet breaks pagination the same way
   (repeated requests at increasing offsets would just re-return page 1,
   looping until `max_listings` is exhausted with only dedup saving it from
   being wrong, not just wasteful). So this fetcher follows the Arcesium/
   Groww "cache the whole pool once" pattern instead of the S&P Global
   Careers "one request per keyword/offset" pattern: one `limit=100` call
   with no keyword, cached in-module, sliced per matcher.py's num/start.
2. The `location=India` query param is a loose text filter, not a strict
   facet — it let 7 non-India (US) jobs through in the same response that
   had 36 genuine India jobs. This fetcher re-checks `country == "India"`
   client-side before keeping anything (same defensive pattern as
   spglobal_careers_fetcher.py's guard, tightened to a strict equality
   check here since `full_location`/`country` are reliably populated).

Each job's `full_location` field already comes back as a clean
"{City}, India" string — no client-side location-string construction needed
(a first in this repo; every other fetcher either lacks a country word or
needs city-name normalisation).

Descriptions are full plain text embedded in the search response (no HTML
tags observed in a live sample of 43 jobs) — `_strip_html` is still applied
defensively since other iCIMS-family tenants in this repo (S&P Global
Careers, Gallagher) do carry markup.

Live data note (2026-08-31): 36 genuine India jobs, all in Hyderabad (30)
and Pune (6) — Pune is excluded by config's `exclude_locations`, so only the
Hyderabad postings are reachable. Titles are specific and signal-rich
("Senior Full Stack Developer", "Senior Database Developer", "Senior Java
Developer", "Senior QA Engineer", "Senior Developer, C++") rather than
generic IT-services level bands, and the one confirmed `title_family` match
in this snapshot (Senior Full Stack Developer, req 12759) has a description
that explicitly names C#, .NET Core, ASP.NET Core, Web API, and SQL — a
genuine .NET/C# match with no ambiguity. `require_tech_in_description` is
NOT enabled, matching the Arcesium precedent: real employer, real titles,
Layer 3 alone is already precise here.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://careers.ice.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"
_LIMIT = 100  # observed safe ceiling; 120+ returns HTTP 422

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/jobs",
}

# Module-level cache: the India pool is fetched once and reused for every
# keyword/page call (offset is ignored server-side; keyword narrows results
# but breaks pagination the same way, so caching the unfiltered pool once —
# like arcesium_fetcher.py/groww_fetcher.py — is both correct and cheap).
_job_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-08-11T06:56:00+0000' -> '2026-08-11'."""
    return raw[:10] if raw else ""


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full ICE India job pool once and cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell lesson
    — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL,
                headers=_HEADERS,
                params={"location": "India", "limit": _LIMIT},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("ICE: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ICE cache fill failed after 3 attempts: {exc}") from exc

    if r is None:
        raise RateLimitError(f"ICE cache fill: no response — {last_exc}")

    raw_jobs = r.json().get("jobs", [])
    collected: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        # location=India is a loose text filter server-side (observed US
        # jobs leaking through) — only keep genuine India postings.
        country = (j.get("country") or "").strip()
        if country.lower() != "india":
            continue

        city = (j.get("city") or "").strip()
        location_str = j.get("full_location") or (f"{city}, India" if city else "India")

        posting_date = _parse_date(j.get("posted_date", ""))
        apply_url = j.get("apply_url") or f"{_BASE_URL}/jobs/{job_id}?lang=en-us"
        description = _strip_html(j.get("description", ""))

        _desc_cache[apply_url] = (description, posting_date)

        collected.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": apply_url,
        })

    _job_cache = collected
    print(f"[ICE] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of ICE India jobs from the cached pool.

    keyword/location are accepted for interface compatibility but ignored:
    the underlying API's offset param doesn't paginate and its keyword
    param, while real, would break pagination the same way — the full pool
    is small enough (~36 India jobs) to cache whole and slice locally.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) — served from the cache populated by fetch_jobs()."""
    _fill_cache(timeout=timeout)
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Fallback: live fetch (should rarely be needed — every job's description
    # is already inline in the search response cached above).
    for attempt in range(3):
        try:
            r = requests.get(
                application_url,
                headers={**_HEADERS, "Accept": "text/html"},
                timeout=timeout,
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
