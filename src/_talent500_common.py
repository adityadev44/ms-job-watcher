"""Shared Talent500 job-fetching logic.

Talent500 (run by ANSR) is a managed GCC hiring platform. Each client
company gets a company-specific page at talent500.com/jobs/<slug>/ backed
by a public JSON API at prod-warmachine.talent500.co.

API endpoint (no auth required, plain GET):

    GET https://prod-warmachine.talent500.co/api/v3/jobs/search/
        ?company_slug=<slug>&offset=0&size=200

Returns {total, data: [{job_code, title_alias_1, slug, location, country,
posted_on, primary_skills, secondary_skills, ...}, ...]}.

Confirmed live 2026-10-03:
- `size=200` fetches all jobs at once for typical GCC boards (<200 jobs).
- Job URL: https://talent500.com/jobs/{job_slug}/
- Job ID: `job_code` field (T500-XXXXX format).
- `location` is already a plain city name ("Hyderabad"), always India.
- `posted_on` is a relative string ("N days ago").
- No server-side keyword filter exists -- `_IGNORES_KEYWORDS` for all
  Talent500 companies (one full-board pass per run).
- Descriptions are NOT in the search response; `primary_skills` and
  `secondary_skills` lists are used as description proxies. Companies
  in this module should have `description_inline=True` so the runner
  uses the inline description (skills string) directly without calling
  `fetch_job_description`. Fetchers raise NotImplementedError for it.
- `require_any_configured_term` (description_filter) should be set True
  for Talent500 companies to filter out non-tech roles (HR, operations,
  procurement, etc.) that appear on the same board as engineering roles.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

import requests

_API_BASE = "https://prod-warmachine.talent500.co"
_JOB_BASE = "https://talent500.com/jobs"
_FETCH_SIZE = 200

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://talent500.com/",
    "Origin": "https://talent500.com",
}


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""


def _parse_posted_on(posted_on: str) -> str:
    """Convert "N days ago" to YYYY-MM-DD."""
    if not posted_on:
        return ""
    s = posted_on.strip().lower()
    today = date.today()
    m = re.search(r"(\d+)\s+day", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+week", s)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    if "today" in s or "just now" in s:
        return today.strftime("%Y-%m-%d")
    if "yesterday" in s:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    return ""


def _get(url: str, params: dict, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Talent500 429 rate-limited: {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Talent500 fetch failed: {exc}") from exc
    raise RateLimitError(f"Talent500: no response -- {last_exc}")


def fetch_jobs_for_company(
    company_slug: str,
    *,
    num: int = 20,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    """Fetch jobs for a Talent500 company slug.

    Ignores keyword/location args (no server-side filtering available).
    Fetches the entire board in one request since all boards are small
    (<200 jobs). `start` is respected for slice compatibility but the
    full fetch always happens on start=0.
    """
    r = _get(
        f"{_API_BASE}/api/v3/jobs/search/",
        {"company_slug": company_slug, "offset": 0, "size": _FETCH_SIZE},
        timeout,
    )

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    for p in r.json().get("data", []):
        job_id = (p.get("job_code") or "").strip()
        if not job_id or job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        title = (p.get("title_alias_1") or p.get("title") or "").strip()
        if not title:
            continue

        loc = (p.get("location") or "").strip()
        country_info = p.get("country") or {}
        country = (country_info.get("name") or "").strip()
        if country and country != "India":
            continue
        location = f"{loc}, India" if loc and "india" not in loc.lower() else loc or "India"

        posting_date = _parse_posted_on(p.get("posted_on", ""))

        slug = (p.get("slug") or "").strip()
        application_url = f"{_JOB_BASE}/{slug}/" if slug else ""

        # Build description proxy from skills lists
        primary = p.get("primary_skills") or []
        secondary = p.get("secondary_skills") or []
        description = " ".join(str(s) for s in primary + secondary if s)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": posting_date,
            "application_url": application_url,
            "description": description,
        })

    return jobs[start: start + num]
