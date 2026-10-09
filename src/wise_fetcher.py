"""Wise official SmartRecruiters board (Wise), verified 2026-10-09.

Country=in returns the India pool. Keywords are deliberately ignored locally:
the full small India pool is paginated once and filtered by the shared matcher.
"""
from __future__ import annotations
import html
import re
import time
import requests

_BASE_URL = "https://api.smartrecruiters.com/v1/companies/Wise/postings"
_PUBLIC_BASE = "https://jobs.smartrecruiters.com/Wise"
_HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36", "Accept": "application/json"}
_desc_cache = {}

class RateLimitError(Exception):
    """SmartRecruiters unavailable after retries."""

def _get(url, timeout, params=None):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Wise request failed: {exc}") from exc
            time.sleep(2 ** attempt)

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    data = _get(_BASE_URL, timeout, {"country": "in", "limit": min(num, 100), "offset": start})
    jobs = []
    for job in data.get("content", []):
        loc = job.get("location") or {}
        if (loc.get("country") or "").lower() != "in":
            continue
        job_id = str(job.get("id") or "")
        if job_id and job.get("name"):
            city = loc.get("city") or ""
            region = "Tamil Nadu" if (loc.get("region") or "").upper() == "TN" else ""
            jobs.append({"id": job_id, "title": job["name"],
                "location": ", ".join(x for x in [city, region, "India"] if x),
                "posting_date": (job.get("releasedDate") or "")[:10],
                "application_url": _PUBLIC_BASE + "/" + job_id})
    return jobs

def fetch_job_description(application_url, timeout=20):
    if application_url not in _desc_cache:
        match = re.search(r"/Wise/(\d+)", application_url or "")
        if not match:
            raise RateLimitError("Wise invalid posting URL")
        job = _get(_BASE_URL + "/" + match.group(1), timeout)
        sections = (job.get("jobAd") or {}).get("sections") or {}
        text = " ".join((sections.get(key) or {}).get("text") or "" for key in ["jobDescription", "qualifications", "additionalInformation"])
        text = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(text)).split())
        _desc_cache[application_url] = (text, (job.get("releasedDate") or "")[:10])
    return _desc_cache[application_url]
