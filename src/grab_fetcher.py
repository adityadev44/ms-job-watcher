"""Fetches Grab job listings from grab.careers.

Grab's careers site is a custom (non-third-party-ATS) server-rendered
platform at www.grab.careers. Confirmed via direct DevTools/HTML
inspection (2026-09-13) — no Greenhouse/Lever/Workday/iCIMS markers
anywhere in the page or its bundled JS.

The listing page (`/en/jobs/`) is plain server-rendered HTML with genuine
server-side filtering: `country=India` narrows to India postings and
`search=<term>` narrows by keyword (both verified live — `search=engineer`
returned 3/7 India results, a nonsense keyword returned 0). Pagination is
`page=N` with a `pagesize` param. No Playwright needed.

Job detail pages embed the full posting as a schema.org JobPosting JSON-LD
block (`<script id="js-job-posting" type="application/ld+json">`) with a
complete HTML `description` field and a `datePosted` field — a clean,
single-request source for both fields fetch_job_description() needs.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://www.grab.careers"
_JOBS_URL = f"{_BASE_URL}/en/jobs/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_MAX_ATTEMPTS = 3
_JOB_HREF_RE = re.compile(r"/en/jobs/(\d+)/")


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from grab.careers."""


def _get(url: str, params: dict | None, timeout: int) -> requests.Response:
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                url, headers=_HEADERS, params=params, timeout=timeout
            )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2**attempt)
                continue
            raise RateLimitError(
                f"Request failed after {_MAX_ATTEMPTS} attempts"
            ) from exc

        if response.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2**attempt)
                continue
            raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")

        response.raise_for_status()
        return response

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 50,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return one page of Grab India job listings matching keyword.

    `country=India` is applied server-side; `search=<keyword>` narrows by
    keyword server-side too. Pagination is page-number based (`page=N`),
    derived here from `start`/`num`.
    """
    page = (start // num) + 1 if num else 1
    params = {
        "country": "India",
        "pagesize": num,
        "page": page,
    }
    if keyword:
        params["search"] = keyword

    response = _get(_JOBS_URL, params, timeout)
    soup = BeautifulSoup(response.text, "html.parser")

    jobs: list[dict[str, str]] = []
    for card in soup.find_all("div", class_="card-job"):
        link = card.find("a", class_="js-view-job", href=_JOB_HREF_RE)
        if not link:
            continue

        href = link.get("href", "")
        id_match = _JOB_HREF_RE.search(href)
        if not id_match:
            continue
        job_id = id_match.group(1)

        title = link.get_text(strip=True)
        application_url = urljoin(_BASE_URL, href)

        location_li = card.find("li", class_="list-inline-item")
        location_str = (
            " ".join(location_li.get_text(separator=" ", strip=True).split())
            if location_li
            else ""
        )

        jobs.append(
            {
                "id": job_id,
                "title": title,
                "location": location_str,
                "posting_date": "",
                "application_url": application_url,
            }
        )

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full description and posting date for a single Grab job.

    Returns (description, posting_date) where posting_date is 'YYYY-MM-DD'.
    Both fields come from the page's embedded schema.org JobPosting JSON-LD
    block, parsed via BeautifulSoup so the HTML-entity-escaped `+` in the
    `type="application/ld+json"` attribute (rendered as `&#x2B;` in raw
    source) is handled transparently.
    """
    response = _get(application_url, None, timeout)
    soup = BeautifulSoup(response.text, "html.parser")

    script = soup.find("script", id="js-job-posting")
    if not script or not script.string:
        return "", ""

    import json

    try:
        data = json.loads(script.string)
    except (ValueError, TypeError):
        return "", ""

    description_html = data.get("description", "") or ""
    description_text = " ".join(
        BeautifulSoup(description_html, "html.parser")
        .get_text(separator=" ", strip=True)
        .split()
    )
    posting_date = data.get("datePosted", "") or ""

    return description_text, posting_date
