"""
Cognizant job fetcher — custom Umbraco CMS career portal (careers.cognizant.com).

Cognizant's career site is a JavaScript-rendered Umbraco application with no
public JSON search API.  However, it exposes a global RSS feed that contains
every active job world-wide (≈2 000 listings) with full HTML descriptions
included in the feed — no per-job HTTP requests needed.

Approach:
  1. Fetch the RSS feed once per process (≈10 MB XML).
  2. Filter to India jobs client-side via the <country> field.
  3. Cache all India jobs and their descriptions at module level so every
     keyword pass shares a single download.
  4. fetch_job_description() is served from cache — zero extra HTTP requests.

ATS: Custom Umbraco CMS  (careers.cognizant.com/global-en/jobs/xml/?rss=true)
"""
from __future__ import annotations

import html as _html_mod
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

_RSS_URL = "https://careers.cognizant.com/global-en/jobs/xml/?rss=true"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/xml,application/xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Module-level cache — populated once per process run.
_cache: list[dict] = []               # India jobs (5-field dicts)
_desc_cache: dict[str, tuple[str, str]] = {}  # url → (description, posting_date)
_cache_filled = False


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from the careers portal."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    """Strip HTML tags, decode entities, normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert RSS date 'Fri, 26 Jun 2026 04:26:34 GMT' → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw.strip())
        return dt.strftime("%Y-%m-%d")
    except Exception:
        # Fallback: extract 4-digit year if parsing fails
        m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if m:
            return m.group(0)
        return ""


def _cdata_text(elem) -> str:
    """Return stripped text from an XML element (CDATA or plain)."""
    return (elem.text or "").strip() if elem is not None else ""


# ---------------------------------------------------------------------------
# Cache builder
# ---------------------------------------------------------------------------

def _fill_cache(timeout: int = 60) -> None:
    """Download the global RSS feed and populate _cache with India jobs."""
    global _cache_filled
    # Set early to prevent retry storms on repeated keyword calls.
    _cache_filled = True

    for attempt in range(3):
        try:
            r = requests.get(_RSS_URL, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Cognizant RSS: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Cognizant RSS fetch failed: {exc}") from exc

    # Parse XML
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        raise RateLimitError(f"Cognizant RSS XML parse error: {exc}") from exc

    india_jobs: list[dict] = []
    for job in root.findall("job"):
        country = _cdata_text(job.find("country"))
        if country.strip().lower() != "india":
            continue

        job_id = (
            _cdata_text(job.find("requisitionid"))
            or _cdata_text(job.find("apijobid"))
        )
        if not job_id:
            continue

        title = _cdata_text(job.find("title"))
        if not title:
            continue

        city  = _cdata_text(job.find("city")).title()    # "PUNE" → "Pune"
        state = _cdata_text(job.find("state"))
        # Build a rich location string so exclude_locations patterns fire
        # correctly (e.g. "Pune, Maharashtra, India" is caught by "Pune").
        if state:
            location = f"{city}, {state}, India"
        else:
            location = f"{city}, India" if city else "India"

        url   = _cdata_text(job.find("url"))
        date  = _parse_date(_cdata_text(job.find("date")))

        raw_desc = _cdata_text(job.find("description"))
        description = _strip_html(raw_desc)

        entry = {
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": date,
            "application_url": url,
        }
        india_jobs.append(entry)
        _desc_cache[url] = (description, date)

    _cache.extend(india_jobs)


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 60,
) -> list[dict]:
    """Return a paginated slice of Cognizant's India jobs.

    The RSS feed does not support server-side keyword or location filtering.
    All India jobs are returned from cache; matcher.py handles title, skill,
    and location filtering downstream.  The keyword parameter is accepted but
    unused so the matcher's keyword-loop still terminates correctly on an
    empty page when start >= len(cache).
    """
    if not _cache_filled:
        _fill_cache(timeout=timeout)

    page = _cache[start : start + num]
    return page


def fetch_job_description(
    application_url: str,
    timeout: int = 60,
) -> tuple[str, str]:
    """Return (description, posting_date) from the RSS cache.

    The full HTML description is embedded in the RSS feed, so no extra HTTP
    request is needed.  Falls back to an empty pair if the URL is not in cache
    (should not happen in normal operation).
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Unexpected miss: fall back to fetching the job page directly.
    for attempt in range(3):
        try:
            r = requests.get(
                application_url,
                headers={**_HEADERS, "Accept": "text/html,*/*"},
                timeout=timeout,
                verify=False,
            )
            if r.status_code == 429:
                raise RateLimitError(f"Cognizant job page: 429 on {application_url}")
            r.raise_for_status()
            description = _strip_html(r.text)
            result = (description, "")
            _desc_cache[application_url] = result
            return result
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    return "", ""
