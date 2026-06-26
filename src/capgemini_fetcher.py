"""Fetches Capgemini job listings via the SAP SuccessFactors J2W HTML search API.

Capgemini's ATS is SAP SuccessFactors J2W hosted at careers.capgemini.com.
The search endpoint accepts plain GET requests with `locationsearch=india` to
pre-filter to India, and `startrow=N` for pagination (25 results per page).
No browser automation needed — plain requests work.

Date in search results: "Jun 26, 2026"  → parsed to YYYY-MM-DD.
Date on detail pages:   "Fri Jun 26 02:00:00 UTC 2026" → parsed the same way.
Location format:        "Bangalore, IN"  → normalised to "Bangalore, India"
                        so matcher.py's is_india_job() recognises it.
"""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.capgemini.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # J2W always returns 25 per page; we cannot change this

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Capgemini J2W."""


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert 'Jun 26, 2026' (search result) to '2026-06-26'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Fri Jun 26 02:00:00 UTC 2026' (meta tag) to '2026-06-26'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Fetch one page of Capgemini India jobs for the given keyword.

    ``start`` maps directly to the J2W ``startrow`` query parameter.
    ``num`` is accepted for API compatibility but J2W always returns 25 per page.
    """
    params: dict = {
        "q": keyword,
        "locationsearch": "india",
    }
    if start:
        params["startrow"] = start

    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL,
                params=params,
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Capgemini J2W: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Capgemini fetch failed: {exc}") from exc

    soup = BeautifulSoup(r.text, "html.parser")
    jobs: list[dict] = []

    for row in soup.select("tr.data-row"):
        # Title + href — prefer the hidden-phone span to avoid duplicate mobile text
        link = row.select_one("span.jobTitle.hidden-phone a.jobTitle-link")
        if not link:
            link = row.select_one("a.jobTitle-link")
        if not link:
            continue

        href = link.get("href", "").strip()
        title = html_mod.unescape(link.get_text(strip=True))
        if not href or not title:
            continue

        # Job ID: trailing numeric segment of the path
        # e.g. "/job/Bangalore-Software-Engineer/1388882733/" → "1388882733"
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id.isdigit():
            continue

        # Location — prefer the non-mobile column cell
        loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
        if not loc_cell:
            loc_cell = row.select_one("span.jobLocation")
        loc_text = ""
        if loc_cell:
            # Strip child elements like <small class="nobr">+14 more…</small>
            # get_text() without separator flattens; use first text node only
            for part in loc_cell.children:
                raw_part = getattr(part, "string", None) or (str(part) if hasattr(part, "strip") else "")
                candidate = raw_part.strip()
                if candidate and not candidate.startswith("+"):
                    loc_text = html_mod.unescape(candidate)
                    break

        # Normalise "Bangalore, IN" → "Bangalore, India"
        loc_text = re.sub(r",\s*IN\b", ", India", loc_text)
        if not loc_text:
            loc_text = "India"

        # Date from search listing: "Jun 26, 2026"
        date_span = row.select_one("td.colDate.hidden-phone span.jobDate")
        if not date_span:
            date_span = row.select_one("span.jobDate")
        posting_date = _parse_search_date(date_span.get_text(strip=True)) if date_span else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc_text,
            "posting_date": posting_date,
            "application_url": f"{_BASE_URL}{href}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the full job description and posting date from the detail page.

    Returns (description_text, posting_date) where posting_date is YYYY-MM-DD.
    The detail page uses <span class="jobdescription"> and
    <meta itemprop="datePosted" content="Fri Jun 26 02:00:00 UTC 2026">.
    """
    for attempt in range(3):
        try:
            r = requests.get(
                application_url,
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                raise RateLimitError(f"Capgemini description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    desc_span = soup.select_one("span.jobdescription")
    description = ""
    if desc_span:
        raw = html_mod.unescape(desc_span.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    date_meta = soup.find("meta", {"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_detail_date(date_meta.get("content", ""))

    return description, posting_date
