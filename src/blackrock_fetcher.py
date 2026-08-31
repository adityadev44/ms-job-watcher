"""Fetches BlackRock job listings from careers.blackrock.com (TalentBrew, org 45831).

Same ATS platform as Optum (careers.unitedhealthgroup.com) and Moody's
(careers.moodys.com), but a THIRD distinct TalentBrew theme:

- Job cards live in ``<li class="section3__search-results-li">`` under
  ``ul.section3__search-results-ul`` (not Optum's plain ``<li>`` nor Moody's
  top-level-only ``<li>`` siblings) -- selected here by CSS class so nesting
  depth doesn't matter.
- The title ``<a data-job-id>`` wraps an ``<h2 class="section3__job-title">``
  (like Optum, unlike Moody's sibling ``<h2>``).
- Location is a nested ``<span class="job-location section3__job-location
  section3__job-information">`` containing a ``"Location:"`` label span
  *and* a ``<span class="section3__job-info">`` with the actual text --
  neither Optum's flat ``<span class="job-location">`` nor Moody's sibling
  ``<li class="job-location">`` pattern, so the value span must be selected
  specifically or the label text leaks into the location string.
- Pagination query param is ``p=`` (Moody's convention), NOT Optum's ``pg=``.
  Page size is 10 (neither Optum's nor Moody's 15).
- ``l=India`` is accepted but ignored server-side (confirmed: identical
  100-job result set regardless of the l= value) -- same as Moody's tenant.
  ``k=`` keyword IS genuinely applied server-side (confirmed: distinct,
  plausible counts per keyword, including "C# developer" -- the ``#``
  character does not break this tenant's search, unlike TCS's iBegin ATS).
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

SEARCH_URL = "https://careers.blackrock.com/search-jobs/"
_BASE_URL = "https://careers.blackrock.com"
_ORG_ID = "45831"
_PAGE_SIZE = 10  # BlackRock's TalentBrew tenant returns 10 results per page

# TalentBrew omits the country name from location strings ("Mumbai, Maharashtra").
# This set lets _parse_card append ", India" so is_india_job() works correctly.
_INDIA_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
    "nagaland", "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "delhi", "jammu and kashmir", "ladakh",
}

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
    """Parse a BlackRock TalentBrew job card into a 5-field dict.

    Location text lives in a nested value span (``.section3__job-info``)
    alongside a sibling ``"Location:"`` label span -- must select the value
    span specifically, not ``get_text()`` the whole location wrapper.
    """
    a_tag = li.select_one("a[data-job-id]")
    if not a_tag:
        return None

    job_id = a_tag.get("data-job-id", "").strip()
    if not job_id:
        return None

    href = a_tag.get("href", "")
    application_url = f"{_BASE_URL}{href}" if href.startswith("/") else href

    h2 = a_tag.select_one("h2")
    title = h2.get_text(strip=True) if h2 else ""

    loc_span = a_tag.select_one("span.job-location .section3__job-info")
    location = loc_span.get_text(strip=True) if loc_span else ""
    if location and any(s in location.lower() for s in _INDIA_STATES):
        location = location + ", India"

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
    """Return one page of BlackRock job listings matching keyword.

    TalentBrew uses page numbers (p=) instead of byte offsets; start is
    converted to p internally. location is hardcoded to India in the request,
    but BlackRock's TalentBrew tenant ignores the l= filter entirely
    (verified: identical 100-job result set for l=India vs no l param at
    all, with genuine non-India cities like New York/London/San Francisco
    mixed in) -- the matcher's is_india_job predicate handles precise
    filtering client-side. keyword IS genuinely applied server-side
    (distinct result counts confirmed per keyword).
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
        for li in ul.select("li.section3__search-results-li"):
            card = _parse_card(li)
            if card:
                jobs.append(card)
        return jobs

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description and posting date for a single BlackRock job.

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
