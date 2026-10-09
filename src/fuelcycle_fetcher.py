"""Fuel Cycle jobs from its verified Ashby public posting API.

2026-10-09: 13 real listings, all structured locations USA. Employer's
careers page confirms its Navi Mumbai hub, so monitor the live board for
future India listings. This is a real adapter, not an empty placeholder.
All descriptions are inline. Job locations, never mentions of the India
office in the company boilerplate, decide geographic eligibility.
"""
from __future__ import annotations
import re
import time
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

_API = "https://api.ashbyhq.com/posting-api/job-board/fuel-cycle"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_jobs = None
_cache_error = None
_descriptions = {}

class RateLimitError(Exception):
    """Ashby could not be fetched after bounded retries."""

def _get(timeout):
    for attempt in range(3):
        try:
            response = requests.get(_API, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Fuel Cycle: {exc}") from exc
            time.sleep(2 ** attempt)

def _india_location(job):
    locations = [job] + (job.get("secondaryLocations") or [])
    india = []
    for loc in locations:
        address = (loc.get("address") or {}).get("postalAddress") or {}
        country = address.get("addressCountry", "").lower()
        label = loc.get("location") or ""
        if country in {"india", "in", "ind"} or (not country and re.search(r"\bindia\b", label, re.I)):
            city = address.get("addressLocality") or label
            region = address.get("addressRegion") or ""
            india.append(", ".join(x for x in [city, region, "India"] if x))
    return "; ".join(india)

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    global _jobs, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _jobs is None:
        _jobs = []
        try:
            data = _get(timeout)
            for job in data.get("jobs", []):
                if not job.get("isListed", True):
                    continue
                job_id = str(job.get("id") or "")
                if not job_id or not job.get("title"):
                    continue
                description = job.get("descriptionPlain") or BeautifulSoup(job.get("descriptionHtml") or "", "html.parser").get_text(" ", strip=True)
                posting_date = (job.get("publishedAt") or "")[:10]
                _descriptions[job_id] = (description, posting_date)
                loc = _india_location(job)
                if not loc:
                    continue
                _jobs.append({"id": job_id, "title": job["title"], "location": loc,
                              "posting_date": posting_date, "application_url": job["jobUrl"]})
        except RateLimitError as exc:
            _cache_error = exc
            raise
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    job_id = urlparse(application_url).path.strip("/").split("/")[-1]
    if job_id not in _descriptions:
        fetch_jobs("", "India", timeout=timeout)
    if job_id not in _descriptions:
        raise RateLimitError("Fuel Cycle: job no longer listed")
    return _descriptions[job_id]
