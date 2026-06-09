"""Fetches Siemens job listings from jobs.siemens.com (iCIMS portal)."""

from __future__ import annotations

import re
import time
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_SEARCH_URL = "https://jobs.siemens.com/en_US/externaljobs/SearchJobs"
# The portal ignores folderRecordsPerPage — it always returns 6 results per page.
_PAGE_SIZE = 6

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# iCIMS detail pages show dates as "01-Jun-2026"
_DATE_RE = re.compile(r"\b(\d{2}-[A-Za-z]{3}-\d{4})\b")


class RateLimitError(Exception):
    """Raised when the site rate-limits after all retries are exhausted."""


def _parse_posted_date(text: str) -> str:
    """Extract a 'DD-Mon-YYYY' date from page text and return 'YYYY-MM-DD'."""
    m = _DATE_RE.search(text)
    if not m:
        return ""
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return one page of Siemens India job listings matching keyword.

    The iCIMS portal encodes keyword and location in the URL path and
    paginates via folderOffset. Page size is fixed at 6 by the portal.
    """
    kw_enc = quote(keyword, safe="")
    loc_enc = quote(location or "India", safe="")
    url = f"{_SEARCH_URL}/{kw_enc}/{loc_enc}/"
    params = {"folderOffset": start}

    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                url, headers=_HEADERS, params=params, timeout=timeout
            )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Request failed after {_MAX_ATTEMPTS} attempts"
            ) from exc

        if response.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")

        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        jobs: list[dict[str, str]] = []

        # Each result is an <article class="article article--result ...">
        for article in soup.find_all("article", class_=re.compile(r"article--result")):
            a = article.find("a", href=re.compile(r"/JobDetail/\d+"))
            if not a:
                continue

            title = a.get_text(strip=True)
            href = a.get("href", "")
            id_match = re.search(r"/JobDetail/(\d+)", href)
            if not id_match:
                continue

            job_id = id_match.group(1)
            application_url = (
                href if href.startswith("http")
                else f"https://jobs.siemens.com{href}"
            )

            loc_span = article.find("span", class_="list-item-location")
            # get_text() (no strip) preserves the ", " text inside separator spans.
            location_str = " ".join(loc_span.get_text().split()) if loc_span else ""

            jobs.append({
                "id": job_id,
                "title": title,
                "location": location_str,
                "posting_date": "",
                "application_url": application_url,
            })

        return jobs

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full description and posting date for a single Siemens job.

    Returns (description, posting_date) where posting_date is 'YYYY-MM-DD'.
    Description lives in <div class="article__content">; posting date is
    extracted via regex from page text (format: DD-Mon-YYYY).
    """
    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Request failed after {_MAX_ATTEMPTS} attempts"
            ) from exc

        if r.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")

        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # article__content only holds metadata (job ID, date, company) — not the
        # description body. main__content / main contains the full posting text.
        content = (
            soup.find("div", class_=re.compile(r"main__content", re.I))
            or soup.find("main")
            or soup.body
        )

        text = " ".join((content or soup).get_text(separator=" ", strip=True).split())
        posting_date = _parse_posted_date(text)
        return text, posting_date

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")
