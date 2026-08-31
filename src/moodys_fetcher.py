"""Fetches Moody's Corporation job listings from careers.moodys.com (TalentBrew).

Same ATS platform as Optum (careers.unitedhealthgroup.com), but a different
TalentBrew theme: job cards use ``search-results-list__*`` class names, the
title link's containing element is an ``<h2>`` (not nested *inside* the
``<a>``), and page location is a top-level ``<li class="job-location">``
sibling rather than a ``<span>`` nested inside the anchor. Pagination also
uses ``p=`` (not ``pg=``) as the query parameter.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

SEARCH_URL = "https://careers.moodys.com/search-jobs"
_BASE_URL = "https://careers.moodys.com"
_ORG_ID = "49841"
_PAGE_SIZE = 15  # TalentBrew returns 15 results per page

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
    """Raised when the API rate-limits after all retries are exhausted."""


def _normalize_date(date_str: str) -> str:
    """Normalize 'YYYY-M-D' (TalentBrew format) to 'YYYY-MM-DD'."""
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) == 3:
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            pass
    return date_str


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_card(li: Any) -> dict[str, str] | None:
    """Parse a Moody's TalentBrew top-level <li> job card into a 5-field dict.

    Unlike Optum's TalentBrew theme, the title <a> is nested *inside* an
    <h2> (not the reverse) and the location <li> is a sibling under
    ``.search-results-list__job-info-list``, not nested inside the <a>.
    """
    a_tag = li.select_one("a[data-job-id]")
    if not a_tag:
        return None

    job_id = a_tag.get("data-job-id", "").strip()
    if not job_id:
        return None

    href = a_tag.get("href", "")
    application_url = f"{_BASE_URL}{href}" if href.startswith("/") else href

    title = a_tag.get_text(strip=True)

    loc_li = li.select_one("li.job-location")
    location = loc_li.get_text(strip=True) if loc_li else ""

    return {
        "id": job_id,
        "title": title,
        "location": location,
        "posting_date": "",  # not available in TalentBrew search results
        "application_url": application_url,
    }


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return one page of Moody's job listings matching keyword.

    TalentBrew uses page numbers (p=) instead of byte offsets; start is
    converted to p internally. location is hardcoded to India in the request
    because Moody's TalentBrew tenant ignores the l= filter entirely
    (verified: identical 128-job result set for l=India, l=Bengaluru, and no
    l param at all) — the matcher's is_india_job predicate handles precise
    filtering client-side.
    """
    p = start // _PAGE_SIZE + 1
    params = {
        "k": keyword,
        "l": "India",
        "orgIds": _ORG_ID,
        "p": p,
        "sort": "postdate",
    }
    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout
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
        ul = soup.select_one("#search-results-list ul")
        if not ul:
            return []

        jobs = []
        for li in ul.find_all("li", recursive=False):
            card = _parse_card(li)
            if card:
                jobs.append(card)
        return jobs

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description and posting date for a single Moody's job.

    Returns a (description, posting_date) tuple. description is plain text;
    posting_date is ISO-formatted 'YYYY-MM-DD' (empty string if unavailable).
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
        ld_script = soup.find("script", type="application/ld+json")
        if ld_script and ld_script.string:
            try:
                ld = json.loads(ld_script.string)
                raw_html = ld.get("description", "")
                posting_date = _normalize_date(ld.get("datePosted", ""))
                description = _strip_html(raw_html) if raw_html else ""
                return description, posting_date
            except (json.JSONDecodeError, AttributeError):
                pass
        return "", ""

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")
