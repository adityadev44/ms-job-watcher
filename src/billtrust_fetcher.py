"""Billtrust India jobs from the verified Greenhouse board billtrust1.

Verified 2026-10-09. The list API supplies full descriptions and ignores
keywords; fetch once and page the India subset. Never use updated_at as
the original posting date. Public branded URLs sometimes use gh_jid.
"""
from __future__ import annotations
import html
import re
import time
from urllib.parse import urlparse, parse_qs
import requests
from bs4 import BeautifulSoup

_BOARD = "billtrust1"
_BASE = f"https://boards-api.greenhouse.io/v1/boards/{_BOARD}/jobs"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_jobs = None
_cache_error = None
_descriptions = {}

class RateLimitError(Exception):
    """The board could not be retrieved after bounded retries."""

def _get(url, timeout):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Billtrust: {exc}") from exc
            time.sleep(2 ** attempt)

def _text(raw):
    return BeautifulSoup(html.unescape(html.unescape(raw or "")), "html.parser").get_text(" ", strip=True)

def _date(job):
    return (job.get("first_published") or "")[:10]

def _id(url):
    query = parse_qs(urlparse(url).query)
    if query.get("gh_jid"):
        return query["gh_jid"][0]
    if query.get("token"):
        return query["token"][0]
    match = re.search(r"/jobs/(\d+)", url)
    return match.group(1) if match else ""

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    global _jobs, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _jobs is None:
        _jobs = []
        try:
            data = _get(_BASE + "?content=true", timeout)
        except RateLimitError as exc:
            _cache_error = exc
            raise
        for job in data.get("jobs", []):
            loc = (job.get("location") or {}).get("name", "")
            if not re.search(r"\bindia\b", loc, re.I):
                continue
            job_id = str(job.get("id") or "")
            if not job_id or not job.get("title"):
                continue
            # Hosted ATS links avoid branded pages whose WAF can block visitors.
            # The hosted board redirects to Billtrust's WAF-blocked domain.
            # Greenhouse's official embed displays the role and application.
            url = f"https://job-boards.greenhouse.io/embed/job_app?for={_BOARD}&token={job_id}"
            _descriptions[job_id] = (_text(job.get("content")), _date(job))
            _jobs.append({"id": job_id, "title": job["title"], "location": loc,
                          "posting_date": _date(job), "application_url": url})
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    job_id = _id(application_url)
    if not job_id:
        raise RateLimitError("Missing Greenhouse job ID")
    if job_id not in _descriptions:
        job = _get(f"{_BASE}/{job_id}?content=true", timeout)
        _descriptions[job_id] = (_text(job.get("content")), _date(job))
    return _descriptions[job_id]
