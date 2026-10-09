"""Harvey's public Ashby posting API, verified against its live board 2026-10-09.

Country fields identify India; secondary locations are retained. Full descriptions
are inline. Keyword/location arguments are ignored by this whole-board adapter.
"""
from __future__ import annotations
import html
import re
import time
import requests

_LIST_URL = "https://api.ashbyhq.com/posting-api/job-board/harvey"
_HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36", "Accept": "application/json"}
_india_cache = []
_content_cache = {}
_cache_filled = False
_cache_error = None

class RateLimitError(Exception):
    """Ashby unavailable after bounded retries."""

def _get(timeout):
    for attempt in range(3):
        try:
            response = requests.get(_LIST_URL, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Harvey board request failed: {exc}") from exc
            time.sleep(2 ** attempt)

def _india_location(job):
    locations = [job] + (job.get("secondaryLocations") or [])
    for item in locations:
        country = ((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")
        location = item.get("location") or ""
        if country.lower() in {"india", "in"} or re.search(r"\bindia\b", location, re.I):
            return location if re.search(r"\bindia\b", location, re.I) else location + ", India"
    return ""

def _fill_cache(timeout):
    global _cache_filled, _cache_error
    if _cache_filled:
        if _cache_error:
            raise _cache_error
        return
    _cache_filled = True
    try:
        for job in _get(timeout)["jobs"]:
            location = _india_location(job)
            if not location or job.get("isListed") is False:
                continue
            job_id = str(job.get("id") or "")
            if not job_id or not job.get("title") or not job.get("jobUrl"):
                continue
            date = (job.get("publishedAt") or "")[:10]
            description = job.get("descriptionPlain") or " ".join(re.sub(r"<[^>]+>", " ", html.unescape(job.get("descriptionHtml") or "")).split())
            _content_cache[job_id] = (description, date)
            _india_cache.append({"id": job_id, "title": job["title"], "location": location,
                "posting_date": date, "application_url": job["jobUrl"]})
    except (RateLimitError, KeyError, TypeError) as exc:
        _cache_error = RateLimitError(f"Harvey board cache failed: {exc}")
        raise _cache_error from exc

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    _fill_cache(timeout)
    return _india_cache[start:start + num]

def fetch_job_description(application_url, timeout=20):
    job_id = (application_url or "").rstrip("/").rsplit("/", 1)[-1]
    _fill_cache(timeout)
    if job_id not in _content_cache:
        raise RateLimitError(f"Harvey posting no longer listed: {job_id}")
    return _content_cache[job_id]
