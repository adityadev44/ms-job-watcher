"""Fetches Charles Schwab India job listings via www.schwabjobs.com.

Backed by iCIMS (career-ind-schwab.icims.com handles the actual Apply flow)
but the public-facing search/listing pages are plain server-rendered HTML —
no REST API or Playwright needed. India is filtered server-side via a URL
path segment: /search-jobs/india, paginated with `&p=N`.

Location in listings is "{City}, {State}" (e.g. "Hyderabad, Telangana") with
no literal "India" — appended client-side since the /india path already
guarantees a genuine India posting. Job-detail pages carry a clean
schema.org JobPosting JSON-LD block with HTML `description` and `datePosted`
("YYYY-M-D", not zero-padded).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE = "https://www.schwabjobs.com"
_LIST_URL = f"{_BASE}/search-jobs/india"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'YYYY-M-D' (not zero-padded) -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        try:
            y, m, d = raw.split("-")
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            return ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Charles Schwab India jobs.

    Pagination is 1-indexed page numbers, not offset-based; only ~19 India
    jobs total exist so pages beyond 2 return empty naturally.
    """
    page_no = (start // 20) + 1
    url = _LIST_URL if page_no == 1 else f"{_LIST_URL}&p={page_no}"

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Charles Schwab: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Charles Schwab fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Charles Schwab fetch: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")
    jobs: list[dict] = []
    for li in soup.select("#search-results-list li"):
        link = li.find("a", href=True)
        if not link:
            continue
        job_id = link.get("data-job-id", "")
        title_el = link.find("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        if not (job_id and title):
            continue
        loc_el = link.find("span", class_="job-location")
        loc = loc_el.get_text(strip=True) if loc_el else "India"
        if "india" not in loc.lower():
            loc = f"{loc}, India"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": "",  # filled in on description fetch (JSON-LD datePosted)
            "application_url": f"{_BASE}{link['href']}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) parsed from the detail page's JSON-LD."""
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Charles Schwab description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Charles Schwab description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Charles Schwab description fetch: no response — {last_exc}")

    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.DOTALL)
    if not m:
        return "", ""

    try:
        data = json.loads(m.group(1))
    except ValueError:
        return "", ""

    description = _strip_html(data.get("description", "") or "")
    posting_date = _parse_date(data.get("datePosted", "") or "")
    return description, posting_date
