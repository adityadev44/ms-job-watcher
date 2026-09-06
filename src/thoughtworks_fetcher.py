"""Fetches ThoughtWorks (thoughtworks.com) India job listings via the
public AEM-backed REST JSON API.

ThoughtWorks' careers section is powered by Adobe Experience Manager (AEM).
The public jobs data endpoint is embedded in the DOM via a `data-jobs-url`
attribute on `.cmp-job-search__container` and confirmed to work without
authentication:

    GET https://www.thoughtworks.com/rest/careers/jobs

Key facts confirmed live 2026-09-06:
  - Returns all 36 global openings in a single response (no pagination,
    no server-side filter params accepted — country param returns 200 but
    with non-JSON body).
  - Only 1 India job present (Bangalore) at last check; historically 1-3.
  - Response structure: {"jobs": [...], "countriesLocationsMap": {...}, ...}
  - Each job: {"name": title, "location": city, "country": "" (unreliable),
    "role": function, "sourceSystemId": int, "updatedAt": ISO 8601}
  - India detection: city "Bangalore" / "Bengaluru" / "Hyderabad" /
    "Mumbai" / "Pune" / "Chennai" / "Noida" / "Gurgaon" / "Kolkata" etc.
    The `country` field is often empty for India jobs; city names are the
    reliable signal.
  - Job URL: https://www.thoughtworks.com/en-us/careers/jobs/{sourceSystemId}
  - Description: `div[class*="description"]` on the individual job page.
  - No date in the API response; `updatedAt` is used as posting_date.

Keywords: NOT applied server-side (single API call returns all global jobs).
The coordinator should add "thoughtworks" to _IGNORES_KEYWORDS in the
registry.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests
from bs4 import BeautifulSoup

_API_URL = "https://www.thoughtworks.com/rest/careers/jobs"
_JOB_BASE_URL = "https://www.thoughtworks.com/en-us/careers/jobs/"

# Known India city names for client-side filtering
_INDIA_CITIES = frozenset({
    "bangalore", "bengaluru", "hyderabad", "mumbai", "pune",
    "chennai", "noida", "gurugram", "gurgaon", "kolkata",
    "delhi", "new delhi", "ahmedabad", "coimbatore", "kochi",
})

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.thoughtworks.com/en-us/careers/jobs",
}

_DETAIL_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from ThoughtWorks."""


# Module-level cache: the API returns all jobs in one call.
_job_cache: list[dict] = []
_cache_filled: bool = False


def _is_india(job: dict) -> bool:
    """Return True if the job is in India (Bangalore/Bengaluru/etc.)."""
    country = (job.get("country") or "").lower()
    if "india" in country:
        return True
    loc = (job.get("location") or "").lower()
    return any(city in loc for city in _INDIA_CITIES)


def _fill_cache(timeout: int = 20) -> None:
    """Fetch all global ThoughtWorks jobs and cache India ones."""
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_API_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("ThoughtWorks: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ThoughtWorks fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"ThoughtWorks fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"ThoughtWorks: non-JSON response — {exc}") from exc

    jobs: list[dict] = []
    seen_ids: set[str] = set()

    for item in data.get("jobs") or []:
        if not _is_india(item):
            continue

        job_id = str(item.get("sourceSystemId") or "").strip()
        title = (item.get("name") or "").strip()
        if not (job_id and title):
            continue
        if job_id in seen_ids:
            continue

        city = (item.get("location") or "").strip()
        location_str = f"{city}, India" if city and city.lower() != "india" else "India"

        # updatedAt: "2026-09-03T02:11:01-04:00" → "2026-09-03"
        posting_date = (item.get("updatedAt") or "")[:10]

        seen_ids.add(job_id)
        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": f"{_JOB_BASE_URL}{job_id}",
        })

    _job_cache[:] = jobs
    print(f"[ThoughtWorks] Cache filled: {len(jobs)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of ThoughtWorks India jobs.

    Keywords are accepted but not applied server-side; the full global
    response is fetched once and India jobs are filtered client-side.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, '') for one ThoughtWorks job detail page.

    Description lives in the first `div[class*="description"]` element that
    contains substantial text. No canonical date is on the detail page (the
    updatedAt date from the API is used instead).
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                application_url, headers=_DETAIL_HEADERS, timeout=timeout
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"ThoughtWorks description: 429 for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ThoughtWorks description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"ThoughtWorks description: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")

    description = ""
    # Look for div elements whose class contains "description"
    for el in soup.find_all("div", class_=re.compile(r"description", re.I)):
        text = html_mod.unescape(el.get_text(" ", strip=True))
        clean = " ".join(text.split())
        if len(clean) > 100:
            description = clean
            break

    return description, ""
