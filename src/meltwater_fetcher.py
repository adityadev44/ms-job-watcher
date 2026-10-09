"""Meltwater's verified public Jobvite board.

Official careers points to Cloudflare-gated Talemetry. Its shipped markup
loads Jobvite's application SDK; jobs.jobvite.com/meltwater is the underlying
live board (same AI Engineer-Team Lead and other current India vacancies).
Verified 2026-10-09: full board on the root, plain HTTP details. Cache the
India pool once; ambiguous multi-location rows are resolved from detail meta.
No original posting date appears on Jobvite; return empty rather than guess.
"""
from __future__ import annotations
import re
import time
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

_BASE = "https://jobs.jobvite.com/meltwater"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
_jobs = None
_cache_error = None
_desc_cache = {}

class RateLimitError(Exception):
    """The employer board failed after bounded retries."""

def _get(url, timeout):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Meltwater: {exc}") from exc
            time.sleep(2 ** attempt)

def _loc(raw):
    return re.sub(r"\s*,\s*", ", ", " ".join(raw.split()))

def fetch_job_description(application_url, timeout=20):
    if application_url not in _desc_cache:
        soup = _get(application_url, timeout)
        body = soup.select_one(".jv-job-detail-description")
        if body is None or len(body.get_text(" ", strip=True)) < 100:
            raise RateLimitError("Meltwater: missing job description")
        _desc_cache[application_url] = (body.get_text(" ", strip=True), "")
    return _desc_cache[application_url]

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    global _jobs, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _jobs is None:
        _jobs = []
        try:
            soup = _get(_BASE, timeout)
            for cell in soup.select("td.jv-job-list-name"):
                link = cell.select_one("a[href]")
                loc_cell = cell.find_next_sibling("td", class_="jv-job-list-location")
                if not link or not loc_cell:
                    continue
                loc = _loc(loc_cell.get_text(" ", strip=True))
                url = urljoin(_BASE, link["href"])
                if re.search(r"\d+ Locations", loc):
                    detail = _get(url, timeout)
                    meta = detail.select_one(".jv-job-detail-meta")
                    if meta:
                        loc = _loc(meta.get_text(" ", strip=True))
                if not re.search(r"\bindia\b", loc, re.I):
                    continue
                _jobs.append({"id": url.rstrip("/").rsplit("/", 1)[-1],
                              "title": link.get_text(" ", strip=True),
                              "location": loc, "posting_date": "",
                              "application_url": url})
        except RateLimitError as exc:
            _cache_error = exc
            raise
    return _jobs[start:start + num]
