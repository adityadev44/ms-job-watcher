"""Observe.AI jobs from its official public Greenhouse board.

The official careers page embeds Greenhouse board ``observeai``.  The
public API returns all postings plus full descriptions in one request; this
fetcher keeps only locations that explicitly contain India/Bengaluru.
"""
from __future__ import annotations

import re
import time
import requests
from bs4 import BeautifulSoup

_API = "https://boards-api.greenhouse.io/v1/boards/observeai/jobs?content=true"
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
                raise RateLimitError(f"Observe.AI fetch failed: {exc}") from exc
            time.sleep(2 ** attempt)
    jobs = []
    for item in response.json().get("jobs", []):
        raw_location = (item.get("location") or {}).get("name", "").strip()
        if not re.search(r"\b(india|bengaluru|bangalore)\b", raw_location, re.I):
            continue
        location = raw_location if "india" in raw_location.lower() else f"{raw_location}, India"
        jid = str(item.get("id", "")).strip()
        title = item.get("title", "").strip()
        if not jid or not title:
            continue
        published = item.get("first_published", "")
        posting_date = published[:10] if re.match(r"\d{4}-\d{2}-\d{2}", published) else ""
        url = item.get("absolute_url", "")
        desc = BeautifulSoup(item.get("content", ""), "html.parser").get_text(" ", strip=True)
        jobs.append({"id": jid, "title": title, "location": location, "posting_date": posting_date, "application_url": url})
        _DESCRIPTIONS[url] = (" ".join(desc.split()), posting_date)
    _CACHE = jobs

def fetch_jobs(keyword: str, location: str, *, num: int = 20, start: int = 0, sort_by: str = "date", timeout: int = 20) -> list[dict[str, str]]:
    _fill(timeout)
    return (_CACHE or [])[start:start + num]

def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    _fill(timeout)
    return _DESCRIPTIONS.get(application_url, ("", ""))
