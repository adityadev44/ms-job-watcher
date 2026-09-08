"""Sarvam AI jobs from Ashby's public job-board API.

The official careers page embeds the same 63 postings exposed at
``api.ashbyhq.com/posting-api/job-board/sarvam``.  Sarvam's board is India
only today (Bengaluru and Delhi); locations are bare city names, so this
fetcher appends ``India`` for the shared location gate.  Descriptions are
included inline and the complete small board is cached once per process.
"""
from __future__ import annotations

import re
import time
import requests
from bs4 import BeautifulSoup

_API = "https://api.ashbyhq.com/posting-api/job-board/sarvam"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"}
_CACHE: list[dict[str, str]] | None = None
_DESCRIPTIONS: dict[str, tuple[str, str]] = {}

class RateLimitError(Exception):
    pass

def _fill(timeout: int) -> None:
    global _CACHE
    if _CACHE is not None:
        return
    response = None
    for attempt in range(3):
        try:
            response = requests.get(_API, headers=_HEADERS, timeout=timeout)
            if response.status_code == 429:
                raise requests.RequestException("429 rate-limited")
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Sarvam AI fetch failed: {exc}") from exc
            time.sleep(2 ** attempt)
    jobs = []
    for item in response.json().get("jobs", []):
        if not item.get("isListed", True):
            continue
        jid = str(item.get("id", "")).strip()
        title = item.get("title", "").strip()
        if not jid or not title:
            continue
        location = item.get("location", "").strip()
        if "india" not in location.lower():
            location = f"{location}, India" if location else "India"
        published = item.get("publishedAt", "")
        posting_date = published[:10] if re.match(r"\d{4}-\d{2}-\d{2}", published) else ""
        url = item.get("jobUrl") or f"https://jobs.ashbyhq.com/sarvam/{jid}"
        description = item.get("descriptionPlain") or BeautifulSoup(item.get("descriptionHtml", ""), "html.parser").get_text(" ", strip=True)
        jobs.append({"id": jid, "title": title, "location": location, "posting_date": posting_date, "application_url": url})
        _DESCRIPTIONS[url] = (" ".join(description.split()), posting_date)
    _CACHE = jobs

def fetch_jobs(keyword: str, location: str, *, num: int = 20, start: int = 0, sort_by: str = "date", timeout: int = 20) -> list[dict[str, str]]:
    _fill(timeout)
    return (_CACHE or [])[start:start + num]

def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    _fill(timeout)
    return _DESCRIPTIONS.get(application_url, ("", ""))
