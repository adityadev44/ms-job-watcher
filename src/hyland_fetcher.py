"""Hyland's official global iCIMS board, linked from hyland.com/en/company/careers.

Verified 2026-10-09: 65 global postings, fourteen India postings. List excerpts are NOT complete
descriptions. Fetch detail HTML, including JSON-LD original publication dates.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

_BASE = "https://careers-hyland.icims.com"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_jobs = None
_failure = None
_details = {}


class RateLimitError(Exception):
    """Source unavailable after bounded retries."""


def _get(url, timeout):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Hyland: {exc}") from exc
            time.sleep(2 ** attempt)


def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    global _jobs, _failure
    if _failure is not None:
        raise RateLimitError(str(_failure))
    if _jobs is None:
        collected = {}
        seen = set()
        try:
            for page in range(100):
                soup = _get(f"{_BASE}/jobs/search?ss=1&pr={page}&in_iframe=1", timeout)
                rows = soup.select(".iCIMS_JobsTable .row")
                if not rows:
                    break
                before = len(seen)
                for row in rows:
                    anchor = row.select_one('a[href*="/jobs/"][href*="/job"]')
                    heading = row.select_one("h3")
                    if not anchor or not heading:
                        continue
                    loc = ""
                    for field in row.select("dt"):
                        if "Job Locations" in field.get_text(" ", strip=True):
                            value = field.find_next_sibling("dd")
                            loc = value.get_text(" ", strip=True) if value else ""
                            break
                    seen.add(anchor.get("href", ""))
                    if not (loc.startswith("IN-") or re.search(r"\bindia\b", loc, re.I)):
                        continue
                    if loc.startswith("IN-"):
                        loc = loc[3:] + ", India"
                    match = re.search(r"/jobs/(\d+)/", anchor.get("href", ""))
                    if not match:
                        continue
                    job_id = match.group(1)
                    collected[job_id] = {"id": job_id, "title": heading.get_text(" ", strip=True),
                                         "location": loc, "posting_date": "",
                                         "application_url": urljoin(_BASE, anchor["href"])}
                if not soup.select_one(f'a[href*="pr={page + 1}"]'):
                    break
                if len(seen) == before:
                    raise RateLimitError("Hyland pagination repeated without progress")
            else:
                raise RateLimitError("Hyland exceeded safe pagination bound")
        except RateLimitError as exc:
            _failure = exc
            raise
        _jobs = list(collected.values())
    return _jobs[max(0, start):max(0, start) + max(0, num)]


def fetch_job_description(application_url, timeout=20):
    if application_url not in _details:
        soup = _get(application_url, timeout)
        body = soup.select_one(".iCIMS_JobContent")
        date = ""
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                record = json.loads(script.get_text())
                if isinstance(record, dict) and record.get("datePosted"):
                    date = record["datePosted"][:10]
            except (ValueError, TypeError):
                continue
        _details[application_url] = (body.get_text(" ", strip=True) if body else "", date)
    return _details[application_url]
