"""India jobs from Workato's verified Greenhouse board (workato), checked 2026-10-09.
The whole board includes descriptions; keyword/location query arguments are ignored.
"""
from __future__ import annotations
import html
import re
import time
import requests

_LIST_URL = "https://boards-api.greenhouse.io/v1/boards/workato/jobs"
_HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36", "Accept": "application/json"}
_india_cache = []
_content_cache = {}
_cache_filled = False
_cache_error = None

class RateLimitError(Exception):
    """Board unavailable after bounded retries."""

def _get(url, timeout):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Workato request failed: {exc}") from exc
            time.sleep(2 ** attempt)

def _text(raw):
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(raw or ""))).split())

def _date(job):
    return (job.get("first_published") or "")[:10]

def _fill_cache(timeout):
    global _cache_filled, _cache_error
    if _cache_filled:
        if _cache_error:
            raise _cache_error
        return
    _cache_filled = True
    try:
        jobs = _get(_LIST_URL + "?content=true", timeout)["jobs"]
        for job in jobs:
            location = (job.get("location") or {}).get("name", "")
            if not re.search(r"\bindia\b", location, re.I):
                continue
            job_id = str(job.get("id") or "")
            if not job_id or not job.get("title") or not job.get("absolute_url"):
                continue
            _content_cache[job_id] = (_text(job.get("content")), _date(job))
            _india_cache.append({"id": job_id, "title": job["title"], "location": location,
                "posting_date": _date(job), "application_url": job["absolute_url"]})
    except (RateLimitError, KeyError, TypeError) as exc:
        _cache_error = RateLimitError(f"Workato board cache failed: {exc}")
        raise _cache_error from exc

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    _fill_cache(timeout)
    return _india_cache[start:start + num]

def fetch_job_description(application_url, timeout=20):
    match = re.search(r"/jobs/(\d+)", application_url or "")
    if not match:
        raise RateLimitError("Workato: invalid job URL")
    job_id = match.group(1)
    if job_id not in _content_cache:
        job = _get(_LIST_URL + "/" + job_id + "?content=true", timeout)
        _content_cache[job_id] = (_text(job.get("content")), _date(job))
    return _content_cache[job_id]
