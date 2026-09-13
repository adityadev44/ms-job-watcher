"""GSK job fetcher — Phenom People CMS (jobs.gsk.com).

Same platform/limitation as Morningstar: Phenom People's own `/widgets`
search API returns `{"status": "failure"}` to a plain unauthenticated
POST (confirmed live 2026-09-13) — it requires browser-side session state
(PLAY_SESSION JWT / CSRF token) that plain HTTP cannot replicate. This
fetcher uses the same two-step workaround as `morningstar_fetcher.py`:

  1. Fetch the sitemap index (`jobs.gsk.com/sitemap.xml` -> two child
     sitemaps under `jobs.gsk.com/gb/en/sitemap{1,2}.xml`) to get every
     live job URL (~688 total confirmed live).
  2. Fetch each job page once and extract JobPosting JSON-LD for
     location, title, date, and description (a WebPage JSON-LD block
     also appears on every page — skipped by `@type` check).

Results are cached at module level so all keyword passes share one scan.
`fetch_job_description()` is served from cache (zero extra HTTP requests).

Confirmed live 2026-09-13: GSK genuinely operates a Bengaluru Global
Capability Centre (see gsk.com/en-gb/careers/our-global-and-regional-hubs)
with real historical software-engineering hiring (e.g. an "Apprentice —
Software Engineer (AI)" and a "Tech Innovation Engineer" posting both
found via web search) — but BOTH of those specific postings had already
closed by the time of this integration (their job-ID URLs 404/redirect to
the homepage with no JobPosting JSON-LD, the same "closed posting, no
error state" behavior documented for Infosys). A full live crawl of all
~688 current global postings found only 10 in India, all non-technical
(Medical Writer, Quality, Sales, Site Engineering) — 0 current SDE/AI
title-family matches is a real, current fact about this snapshot (GSK's
tech pipeline appears to churn in bursts, not a steady drip), not a
fetcher defect, the same class of finding documented for Novartis/Pfizer.
DO NOT add `require_tech_in_description` — there's no title-family match
to even reach that layer yet, and the real blocker is total volume, not
an overly-broad skill list.

India detection: JSON-LD `jobLocation.address.addressCountry == "India"`
(authoritative — GSK's JSON-LD is clean and consistent, no city-only or
state-only rows observed, unlike Novartis/MSD).
"""
from __future__ import annotations

import html as _html_mod
import json
import re
import time

import requests

_SITEMAP_INDEX = "https://jobs.gsk.com/sitemap.xml"
_SITEMAP_FALLBACK = [
    "https://jobs.gsk.com/gb/en/sitemap1.xml",
    "https://jobs.gsk.com/gb/en/sitemap2.xml",
]
_JOB_URL_RE = re.compile(r"https://jobs\.gsk\.com/gb/en/job/\d+/[^\s<]*")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Module-level cache — filled on first fetch_jobs() call.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled = False


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_job_page(html_text: str, url: str) -> dict | None:
    """Extract job data from a Phenom People job page via JSON-LD.

    Returns a job dict (with an internal _description key) or None if the
    posting is closed (no JobPosting block present — GSK's closed jobs
    silently redirect/render the homepage with no error), not India-based,
    or otherwise unparseable.
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

        if "india" not in country.lower():
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
    """Fetch the sitemap index + every job page once, cache India jobs."""
    global _cache, _desc_cache

    job_urls: list[str] = []
    try:
        r = requests.get(_SITEMAP_INDEX, headers=_HEADERS, timeout=timeout, verify=False)
        r.raise_for_status()
        child_sitemaps = re.findall(r"<loc>([^<]+)</loc>", r.text)
        if not child_sitemaps:
            child_sitemaps = _SITEMAP_FALLBACK
    except Exception:
        child_sitemaps = _SITEMAP_FALLBACK

    for sm_url in child_sitemaps:
        for attempt in range(3):
            try:
                rs = requests.get(sm_url, headers=_HEADERS, timeout=timeout, verify=False)
                if rs.status_code == 429:
                    raise RateLimitError(f"429 on {sm_url}")
                rs.raise_for_status()
                job_urls.extend(_JOB_URL_RE.findall(rs.text))
                break
            except RateLimitError:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise RateLimitError(f"GSK sitemap fetch failed: {exc}") from exc
                time.sleep(2 ** attempt)

    for url in job_urls:
        time.sleep(0.1)
        try:
            rj = requests.get(url, headers=_HEADERS, timeout=timeout, verify=False)
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
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of GSK India jobs.

    All keywords produce the same full India job set (cached after first
    call, same pattern as Morningstar). Pagination via start/num slices
    the cache list.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before try to avoid retry storm on failure
        _fill_cache(timeout=timeout)

    return _cache[start: start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a GSK job.

    Served from the in-memory cache built during fetch_jobs(); no extra
    HTTP request is made unless the URL is missing from cache (rare).
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                raise RateLimitError(f"429 on {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception:
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
