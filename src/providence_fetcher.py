"""Providence India's official SuccessFactors J2W board, checked 2026-10-09.

Search returns 15 data-row entries per page with startrow offsets. Fetches
the complete board once, newest first; country IN normalized explicitly.
"""
from __future__ import annotations
import re
import time
from datetime import datetime
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.providence.in"
_HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36"}
_india_cache = []
_description_cache = {}
_cache_filled = False
_cache_error = None

class RateLimitError(Exception):
    """SuccessFactors unavailable after retries."""

def _get(url, timeout, params=None):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Providence request failed: {exc}") from exc
            time.sleep(2 ** attempt)

def _date(raw):
    raw = " ".join(raw.split()).replace("Sept ", "Sep ")
    for fmt in ["%b %d, %Y", "%a %b %d %H:%M:%S UTC %Y"]:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""

def _fill_cache(timeout):
    global _cache_filled, _cache_error
    if _cache_filled:
        if _cache_error:
            raise _cache_error
        return
    _cache_filled = True
    seen = set()
    try:
        for offset in range(0, 3000, 15):
            soup = _get(_BASE_URL + "/search/", timeout, {"startrow": offset, "sortColumn": "referencedate", "sortDirection": "desc"})
            rows = soup.select("tr.data-row")
            new = 0
            for row in rows:
                link = row.select_one("a.jobTitle-link[href]")
                loc = row.select_one(".jobLocation")
                date = row.select_one(".jobDate")
                if link is None or loc is None:
                    continue
                url = urljoin(_BASE_URL, link["href"])
                job_id = url.rstrip("/").rsplit("/", 1)[-1]
                if job_id in seen:
                    continue
                seen.add(job_id)
                new += 1
                location = loc.get_text(" ", strip=True)
                location = re.sub(r",\s*IN$", ", India", location)
                if not re.search(r"\bindia\b", location, re.I):
                    continue
                _india_cache.append({"id": job_id, "title": link.get_text(" ", strip=True), "location": location,
                    "posting_date": _date(date.get_text(" ", strip=True)) if date else "", "application_url": url})
            if not new:
                break
        else:
            raise RateLimitError("Providence pagination exceeds 3000 listings")
    except RateLimitError as exc:
        _cache_error = exc
        raise

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    _fill_cache(timeout)
    return _india_cache[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _description_cache:
        soup = _get(application_url, timeout)
        description = soup.select_one(".jobdescription")
        if description is None:
            raise RateLimitError("Providence detail lacks job description")
        date = soup.select_one('meta[itemprop="datePosted"]')
        _description_cache[application_url] = (description.get_text(" ", strip=True), _date(date.get("content", "")) if date else "")
    return _description_cache[application_url]
