"""
MetLife job fetcher — Avature career portal (metlifecareers.com).

Uses plain HTTP POST (no Playwright needed).  The portal renders job listings
server-side; pagination is driven by the ``offset`` field.

India filter: country field 12310[] = 116642 (MetLife's Avature ID for India).

Keyword behaviour: MetLife's search engine does not reliably narrow results for
tech keywords (e.g. ".NET developer" returns 0 results).  fetch_jobs accepts the
keyword parameter but always fetches all India jobs and lets the caller's title/
skills filters handle relevance.  Callers should use a single empty keyword in
their config (keywords: [""]) to get one clean sweep of all ~95 India jobs.
"""
from __future__ import annotations

import html as _html_mod
import re
import time
from datetime import datetime

import requests

_BASE_URL = "https://www.metlifecareers.com"
_SEARCH_URL = f"{_BASE_URL}/en_US/ml/SearchJobs"
_INDIA_COUNTRY_ID = 116642
_PAGE_SIZE = 6

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin": _BASE_URL,
    "Referer": _SEARCH_URL,
}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(date_str: str) -> str:
    """Convert 'DD-Mon-YYYY' → 'YYYY-MM-DD'; return '' on failure."""
    try:
        return datetime.strptime(date_str.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return date_str.strip()


def _make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    # Establish a cookie session before POSTing
    sess.get(_SEARCH_URL, timeout=15)
    return sess


_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _make_session()
    return _SESSION


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    """Fetch one page of India MetLife jobs.

    ``keyword`` is accepted for API compatibility but ignored — all India jobs
    are fetched each call and the title/skills filters handle relevance.
    ``num`` is capped at _PAGE_SIZE (6) because the portal only returns 6 per page.
    """
    sess = _get_session()
    data: dict[str, str] = {
        "jobSort": "",
        "jobSortDirection": "",
        "listFilterMode": "true",
        "12310[]": str(_INDIA_COUNTRY_ID),
        "offset": str(start),
    }

    for attempt in range(3):
        try:
            r = sess.post(_SEARCH_URL, data=data, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"MetLife search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    html = r.text

    # Parse job articles
    articles = re.findall(r'<article[^>]*class="[^"]*article--result[^"]*"[^>]*>([\s\S]*?)</article>', html)

    jobs = []
    for art in articles:
        # Title and URL
        link_m = re.search(r'href="([^"]+/JobDetail/[^"]+)"[^>]*>\s*([^<]+)\s*</a>', art)
        if not link_m:
            continue
        url = link_m.group(1).strip()
        title = _html_mod.unescape(link_m.group(2).strip())

        # Job ID from URL: /JobDetail/SLUG/12345 → 12345
        id_m = re.search(r'/JobDetail/[^/]+/(\d+)', url)
        if not id_m:
            continue
        job_id = id_m.group(1)

        # Location
        loc_m = re.search(r'<span class="list-item-location">([^<]+)</span>', art)
        location_str = _html_mod.unescape(loc_m.group(1).strip()) if loc_m else ""

        # Only keep India locations
        if "india" not in location_str.lower():
            continue

        # Posted date
        date_m = re.search(r'<span class="list-item-posted">([^<]+)</span>', art)
        raw_date = date_m.group(1).strip() if date_m else ""
        posting_date = _parse_date(raw_date) if raw_date else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description and posting date from the detail page.

    Returns (description_text, posting_date_YYYY-MM-DD).
    """
    sess = _get_session()

    for attempt in range(3):
        try:
            r = sess.get(application_url, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on detail for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    html = r.text

    # Posted date from the General Information article content section
    # Pattern: "Posted Date 04-Jun-2026"
    date_m = re.search(r'Posted\s+Date\s+(\d{2}-[A-Za-z]{3}-\d{4})', html)
    posting_date = _parse_date(date_m.group(1)) if date_m else ""

    # Description: the large "Description and Requirements" article
    # Find all article__content divs and take the longest one (the description)
    content_sections = re.findall(
        r'<div class="article__content">([\s\S]*?)</div>\s*</div>\s*</article>',
        html
    )
    description = ""
    for section in content_sections:
        text = _strip_html(section)
        if len(text) > len(description):
            description = text

    return description, posting_date
