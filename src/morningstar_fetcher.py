"""
Morningstar job fetcher — Phenom People CMS (careers.morningstar.com).

Phenom People's job search API is not accessible without client-side JS
execution, so this fetcher uses a two-step approach:

  1. Fetch the sitemap.xml to get all job URLs (single request).
  2. Fetch each job page once and extract JSON-LD structured data for
     location, title, date, and description.

Results are cached at module level so all keyword passes share one scan.
fetch_job_description() is served from cache (zero extra HTTP requests).

All Morningstar India jobs are in Navi Mumbai; location is taken directly
from the JSON-LD addressLocality + addressCountry fields.
"""
from __future__ import annotations

import html as _html_mod
import json
import re
import time

import requests

_SITEMAP_URL = "https://careers.morningstar.com/us/en/sitemap.xml"
_JOB_URL_PREFIX = "https://careers.morningstar.com/us/en/job/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_INDIA_CITIES = {
    "navi mumbai", "mumbai", "gurugram", "gurgaon",
    "bengaluru", "bangalore", "hyderabad", "noida",
    "pune", "chennai", "kolkata",
}

# Module-level cache — filled on first fetch_jobs() call.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}  # url → (description, posting_date)
_cache_filled = False


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_job_page(html_text: str, url: str) -> dict | None:
    """Extract job data from a Phenom People job page via JSON-LD.

    Returns a job dict (with _description key) or None if not an India job
    or if parsing fails.
    """
    blocks = re.findall(
        r'<script[^>]+application/ld\+json[^>]*>([\s\S]*?)</script>',
        html_text,
    )
    for raw_block in blocks:
        try:
            d = json.loads(raw_block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get("@type") != "JobPosting":
            continue

        addr = d.get("jobLocation", {}).get("address", {})
        country = addr.get("addressCountry", "")
        city = addr.get("addressLocality", "")

        # Keep only India jobs
        is_india = (
            "india" in country.lower()
            or "india" in city.lower()
            or city.lower() in _INDIA_CITIES
        )
        if not is_india:
            return None

        job_id = d.get("identifier", {}).get("value", "")
        title = d.get("title", "").strip()
        date = d.get("datePosted", "")
        location_str = f"{city}, India" if city else "India"
        description = _strip_html(d.get("description", ""))

        if not job_id or not title:
            return None

        return {
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": date,
            "application_url": url,
            "_description": description,
        }
    return None


def _fill_cache(timeout: int = 20) -> None:
    """Fetch sitemap + every job page once, cache India jobs."""
    global _cache, _desc_cache

    # Fetch sitemap for all job URLs
    for attempt in range(3):
        try:
            r = requests.get(_SITEMAP_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("429 on sitemap")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"Sitemap fetch failed: {exc}") from exc
            time.sleep(2 ** attempt)

    job_urls = re.findall(
        r'<loc>(' + re.escape(_JOB_URL_PREFIX) + r'[^<]+)</loc>',
        r.text,
    )

    for url in job_urls:
        time.sleep(0.2)
        try:
            rj = requests.get(url, headers=_HEADERS, timeout=timeout)
            if rj.status_code == 410:  # job filled / removed
                continue
            if rj.status_code != 200:
                continue
            job = _parse_job_page(rj.text, url)
            if job:
                desc = job.pop("_description", "")
                _cache.append(job)
                _desc_cache[url] = (desc, job["posting_date"])
        except Exception:
            continue


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Morningstar India jobs.

    All keywords produce the same full India job set (cached after first
    call). Pagination via start/num slices the cache list.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before try to avoid retry storm on failure
        _fill_cache(timeout=timeout)

    return _cache[start: start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a Morningstar job.

    Served from the in-memory cache built during fetch_jobs(); no extra
    HTTP request is made unless the URL is missing from cache (rare).
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Fallback: live fetch (should rarely be needed)
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    job = _parse_job_page(r.text, application_url)
    if job:
        desc = job.get("_description", "")
        date = job.get("posting_date", "")
        _desc_cache[application_url] = (desc, date)
        return desc, date
    return "", ""
