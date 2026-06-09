"""Fetches Honeywell job listings from careers.honeywell.com (Phenom People ATS)."""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_CAREERS_URL = "https://careers.honeywell.com/en/sites/Honeywell/jobs"
_WIDGETS_URL = "https://careers.honeywell.com/widgets"
_INDIA_LOCATION_ID = "300000000469485"
_PAGE_SIZE = 20

_HEADERS_HTML = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://careers.honeywell.com/en/sites/Honeywell/jobs",
}

_HEADERS_JSON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": "https://careers.honeywell.com/en/sites/Honeywell/jobs",
}

# Cached per-process — only fetched once from the careers page HTML.
_ref_num_cache: str | None = None

_REF_NUM_RE = re.compile(r'"refNum"\s*:\s*"([^"]+)"')


class RateLimitError(Exception):
    """Raised when the site rate-limits or blocks after all retry attempts."""


def _get_ref_num(timeout: int = 20) -> str:
    """Fetch and cache the Phenom `refNum` required for all /widgets requests."""
    global _ref_num_cache
    if _ref_num_cache:
        return _ref_num_cache

    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.get(_CAREERS_URL, headers=_HEADERS_HTML, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Failed to fetch careers page: {exc}") from exc

        if r.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError("Rate-limited fetching careers page")

        r.raise_for_status()
        m = _REF_NUM_RE.search(r.text)
        if not m:
            raise RateLimitError(
                "Could not extract refNum from Honeywell careers page — "
                "portal may have changed structure or is blocking requests"
            )
        _ref_num_cache = m.group(1)
        return _ref_num_cache

    raise RateLimitError("Failed to fetch careers page after retries")


def _parse_posted_date(raw: str) -> str:
    """Normalise various date formats to YYYY-MM-DD; return '' on failure."""
    if not raw:
        return ""
    normalised = " ".join(raw.split())
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y"):
        try:
            return datetime.strptime(normalised[:len(fmt) + 4], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Last-resort: try stripping trailing timezone or milliseconds
    try:
        return datetime.strptime(normalised[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return one page of Honeywell India job listings.

    location param is ignored — India is hardcoded via locationId in the POST
    body (consistent with how amazon_fetcher and optum_fetcher handle location).
    Phenom's keyword filter is server-side and works reliably.
    """
    ref_num = _get_ref_num(timeout=timeout)

    payload = {
        "refNum": ref_num,
        "ddoKey": "refineSearch",
        "from": start,
        "size": num,
        "lang": "en_global",
        "deviceType": "desktop",
        "pageName": "search-results",
        "sort": {"order": "desc", "field": "postedDate"},
        "locationData": {
            "locationId": _INDIA_LOCATION_ID,
            "locationLevel": "country",
        },
        "keyword": keyword,
    }

    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.post(
                _WIDGETS_URL, headers=_HEADERS_JSON, json=payload, timeout=timeout
            )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"POST /widgets failed: {exc}") from exc

        if r.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError("Rate-limited on POST /widgets")

        r.raise_for_status()

        data = r.json()
        jobs_raw = (
            data.get("refineSearch", {})
                .get("data", {})
                .get("jobs") or []
        )

        results: list[dict[str, str]] = []
        for job in jobs_raw:
            job_id = str(job.get("jobId", job.get("reqId", "")))
            if not job_id:
                continue

            apply_url = job.get("applyUrl", "")
            if not apply_url:
                job_seq = job.get("jobSeqNo", "")
                title_slug = re.sub(r"[^a-z0-9]+", "-", job.get("title", "").lower()).strip("-")
                apply_url = f"https://careers.honeywell.com/en/sites/Honeywell/job/{job_seq}/{title_slug}"

            loc = job.get("location", "")
            if "india" not in loc.lower():
                loc = f"{loc}, India".strip(", ")

            results.append({
                "id": job_id,
                "title": job.get("title", ""),
                "location": loc,
                "posting_date": _parse_posted_date(job.get("postedDate", "")),
                "application_url": apply_url,
            })
        return results

    raise RateLimitError("POST /widgets failed after retries")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch full description and posting date for a single Honeywell job.

    Returns (description, posting_date). Tries JSON-LD first (like optum_fetcher),
    falls back to <main> body text (like siemens_fetcher).
    """
    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.get(application_url, headers=_HEADERS_HTML, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Failed to fetch job detail: {exc}") from exc

        if r.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError("Rate-limited fetching job detail")

        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # Try JSON-LD first (Phenom embeds structured data on detail pages)
        ld_script = soup.find("script", type="application/ld+json")
        if ld_script and ld_script.string:
            try:
                ld = json.loads(ld_script.string)
                raw_html = ld.get("description", "")
                posting_date = _parse_posted_date(ld.get("datePosted", ""))
                description = _strip_html(raw_html) if raw_html else ""
                if description:
                    return description, posting_date
            except (json.JSONDecodeError, AttributeError):
                pass

        # Fallback: extract from <main> body text
        content = soup.find("main") or soup.body
        text = " ".join((content or soup).get_text(separator=" ", strip=True).split())
        return text, ""

    raise RateLimitError("Failed to fetch job detail after retries")
