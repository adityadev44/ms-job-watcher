"""Codec's verified Pinpoint board.

Official codec.ie/careers renders Pinpoint job links dynamically; plain
HTML omits them. Verified 2026-10-09 in Firefox, then public postings.json
returns all 17 jobs with descriptions/requirements. Current UK/Ireland
locations produce zero India jobs. Monitor future employer-owned India
postings without relabelling Trigent partner vacancies as Codec.
"""
from __future__ import annotations
import re
import time
import requests
from bs4 import BeautifulSoup

_API = "https://codec.pinpointhq.com/postings.json"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_jobs = None
_cache_error = None
_descriptions = {}

class RateLimitError(Exception):
    """The live board failed after bounded retries."""

def _get(timeout):
    for attempt in range(3):
        try:
            response = requests.get(_API, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Codec: {exc}") from exc
            time.sleep(2 ** attempt)

def _location(post):
    loc = post.get("location") or {}
    parts = list(dict.fromkeys(x for x in [loc.get("city"), loc.get("province"), loc.get("name")] if x))
    text = ", ".join(parts)
    if re.search(r"\bindia\b", text, re.I):
        return text
    # India delivery cities may be published without a country label.
    if re.search(r"\b(?:bengaluru|bangalore|hyderabad|mumbai|gurugram|gurgaon|noida|pune|chennai)\b", text, re.I):
        return text + ", India"
    return ""

def _description(post):
    return " ".join(BeautifulSoup(post.get(key) or "", "html.parser").get_text(" ", strip=True)
                    for key in ["description", "key_responsibilities", "skills_knowledge_expertise"])

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    global _jobs, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _jobs is None:
        _jobs = []
        try:
            for post in _get(timeout).get("data", []):
                url = post.get("url") or ""
                jid = str(post.get("id") or "")
                if not url or not jid or not post.get("title"):
                    continue
                _descriptions[url] = (_description(post), "")
                loc = _location(post)
                if not loc:
                    continue
                _jobs.append({"id": jid, "title": post["title"], "location": loc,
                              "posting_date": "", "application_url": url})
        except RateLimitError as exc:
            _cache_error = exc
            raise
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _descriptions:
        fetch_jobs("", "India", timeout=timeout)
    if application_url not in _descriptions:
        raise RateLimitError("Codec: job no longer listed")
    return _descriptions[application_url]
