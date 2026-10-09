"""Redwood Software's official Greenhouse board, linked from redwood.com/careers.

Verified 2026-10-09: 34 global postings, 12 India postings. Full descriptions
are inline. Keywords are ignored; cache the board and slice the India subset.
"""
from __future__ import annotations

import html
import re
import time

import requests
from bs4 import BeautifulSoup

_BASE = "https://boards-api.greenhouse.io/v1/boards/redwoodsoftware/jobs"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_jobs = None
_descriptions = {}
_failure = None


class RateLimitError(Exception):
    """Source unavailable after bounded retries."""


def _get(url, timeout):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Redwood Software: {exc}") from exc
            time.sleep(2 ** attempt)


def _record(job):
    date = (job.get("first_published") or "")[:10]
    text = BeautifulSoup(html.unescape(html.unescape(job.get("content") or "")), "html.parser").get_text(" ", strip=True)
    return text, date


def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    global _jobs, _failure
    if _failure is not None:
        raise RateLimitError(str(_failure))
    if _jobs is None:
        try:
            data = _get(_BASE + "?content=true", timeout)
        except RateLimitError as exc:
            _failure = exc
            raise
        collected = []
        for job in data.get("jobs", []):
            loc = (job.get("location") or {}).get("name", "")
            job_id = str(job.get("id") or "")
            if not re.search(r"\bindia\b", loc, re.I) or not job_id or not job.get("title"):
                continue
            _descriptions[job_id] = _record(job)
            collected.append({"id": job_id, "title": job["title"], "location": loc,
                              "posting_date": _descriptions[job_id][1],
                              "application_url": f"https://job-boards.greenhouse.io/redwoodsoftware/jobs/{job_id}"})
        _jobs = collected
    return _jobs[max(0, start):max(0, start) + max(0, num)]


def fetch_job_description(application_url, timeout=20):
    match = re.search(r"/jobs/(\d+)", application_url)
    if not match:
        raise RateLimitError("Missing Redwood Greenhouse job ID")
    job_id = match.group(1)
    if job_id not in _descriptions:
        _descriptions[job_id] = _record(_get(f"{_BASE}/{job_id}?content=true", timeout))
    return _descriptions[job_id]
