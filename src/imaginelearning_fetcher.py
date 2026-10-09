"""Imagine Learning's official Jobvite board, verified 2026-10-09.

The official careers page links this tenant. Its current search layout uses
span rows, not Nutanix's table theme. Board pagination is 50 rows per page.
India city/state locations omit the country; only verified Bengaluru/Karnataka
or explicit India locations are normalized. Ambiguous multi-site rows are
resolved from their detail header before applying country filters.
"""
from __future__ import annotations
import re
import time
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.jobvite.com/imagine-learning"
_HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36"}
_india_cache = []
_description_cache = {}
_cache_filled = False
_cache_error = None

class RateLimitError(Exception):
    """Jobvite unavailable after bounded retries."""

def _get(url, timeout):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Imagine Learning request failed: {exc}") from exc
            time.sleep(2 ** attempt)

def _normalize_location(raw):
    raw = " ".join(raw.split())
    if re.search(r"\bindia\b", raw, re.I):
        return raw
    if re.search(r"\b(bengaluru|bangalore|karnataka)\b", raw, re.I):
        return raw + ", India"
    return ""

def _detail(url, timeout):
    soup = _get(url, timeout)
    description = soup.select_one(".jv-job-detail-description")
    if description is None:
        raise RateLimitError("Imagine Learning detail missing description")
    _description_cache[url] = (description.get_text(" ", strip=True), "")
    return soup

def _fill_cache(timeout):
    global _cache_filled, _cache_error
    if _cache_filled:
        if _cache_error:
            raise _cache_error
        return
    _cache_filled = True
    seen = set()
    try:
        for page in range(100):
            soup = _get(_BASE_URL + "/search?p=" + str(page), timeout)
            rows = soup.select(".jv-search-list a[href]")
            new = 0
            for link in rows:
                title = link.select_one(".jv-job-list-name")
                loc = link.select_one(".jv-job-list-location")
                if title is None or loc is None:
                    continue
                url = urljoin(_BASE_URL, link["href"])
                job_id = url.rstrip("/").rsplit("/", 1)[-1]
                if job_id in seen:
                    continue
                seen.add(job_id)
                new += 1
                raw = loc.get_text(" ", strip=True)
                if re.search(r"\d+ Locations", raw, re.I):
                    detail = _detail(url, timeout)
                    header = detail.select_one(".jv-job-detail-meta")
                    raw = header.get_text(" ", strip=True) if header else ""
                location = _normalize_location(raw)
                if location:
                    _india_cache.append({"id": job_id, "title": title.get_text(" ", strip=True),
                        "location": location, "posting_date": "", "application_url": url})
            if not new:
                break
        else:
            raise RateLimitError("Imagine Learning pagination exceeded 100 pages")
    except RateLimitError as exc:
        _cache_error = exc
        raise

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    _fill_cache(timeout)
    return _india_cache[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _description_cache:
        _detail(application_url, timeout)
    return _description_cache[application_url]
