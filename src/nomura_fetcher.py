"""
Nomura job fetcher — SAP SuccessFactors J2W HTML scraping.

India portal: careers.nomura.com/Nomura/go/Career-Opportunities-India/9050900/
All India jobs fetched and cached in-module; descriptions fetched on demand.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.nomura.com"
_INDIA_PORTAL_BASE = f"{_BASE_URL}/Nomura/go/Career-Opportunities-India/9050900"
_INDIA_PORTAL_PARAMS = "?q=&sortColumn=referencedate&sortDirection=desc"
_PAGE_SIZE = 100  # SuccessFactors J2W default page size

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# Module-level caches — filled once per process run.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled = False


class RateLimitError(Exception):
    pass


def _parse_date(raw: str) -> str:
    """Convert 'Sat Jun 13 07:00:00 UTC 2026' to '2026-06-13'."""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through all India Nomura jobs and fill _cache."""
    start = 0
    seen_ids: set[str] = set()

    while True:
        # Path-based pagination: /9050900/ for page 1, /9050900/100/ for page 2, etc.
        if start == 0:
            url = f"{_INDIA_PORTAL_BASE}/{_INDIA_PORTAL_PARAMS}"
        else:
            url = f"{_INDIA_PORTAL_BASE}/{start}/{_INDIA_PORTAL_PARAMS}"
        for attempt in range(3):
            try:
                r = requests.get(url, headers=_HEADERS, timeout=timeout)
                if r.status_code == 429:
                    raise RateLimitError("429 on Nomura India portal")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise RateLimitError(f"Nomura fetch failed: {exc}") from exc
                time.sleep(2 ** attempt)

        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("tr.data-row")
        if not rows:
            break

        new_this_page = 0
        for row in rows:
            link = row.select_one("a.jobTitle-link")
            if not link:
                continue
            href = link.get("href", "")
            title = html_mod.unescape(link.get_text(strip=True))
            if not href or not title:
                continue

            # Job ID is the trailing numeric segment of the path
            job_id = href.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit():
                continue
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            loc_span = row.select_one("span.jobLocation")
            loc_text = html_mod.unescape(loc_span.get_text(strip=True)) if loc_span else ""
            # Location format is "Mumbai, IN" — convert so is_india_job() recognises it
            loc_text = re.sub(r",\s*IN\b", ", India", loc_text)

            _cache.append({
                "id": job_id,
                "title": title,
                "location": loc_text,
                "posting_date": "",  # populated by fetch_job_description
                "application_url": f"{_BASE_URL}{href}",
            })
            new_this_page += 1

        if new_this_page < _PAGE_SIZE:
            break
        start += _PAGE_SIZE
        time.sleep(0.3)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True
        _fill_cache(timeout=timeout)
    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on Nomura detail: {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                _desc_cache[application_url] = ("", "")
                return "", ""
            time.sleep(2 ** attempt)

    soup = BeautifulSoup(r.text, "html.parser")

    desc_span = soup.select_one("span.jobdescription")
    description = html_mod.unescape(desc_span.get_text(" ", strip=True)) if desc_span else ""

    posting_date = ""
    date_meta = soup.find("meta", {"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_date(date_meta.get("content", ""))

    _desc_cache[application_url] = (description, posting_date)
    return description, posting_date
