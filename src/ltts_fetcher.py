"""Fetches L&T Technology Services (LTTS, jobs.ltts.com) India job listings
via the SAP SuccessFactors Job2Web (J2W) HTML search endpoint.

LTTS runs the classic (non-Unify) J2W platform at jobs.ltts.com — the same
family as Mastek/Capgemini, but with the `<tr class="data-row">` tile style.

Key facts confirmed live 2026-09-06:
  - GET /search/ with `q=<keyword>` and `locationsearch=india` returns
    server-filtered results: "software engineer" → 7 rows, nonsense → 0 rows
    → keywords ARE applied server-side (unlike Mastek/Capgemini).
  - 9 India jobs total fit on a single page (no startrow pagination needed
    at current volume, but the fetcher supports it for future growth using
    the J2W standard `startrow=N` param).
  - Each row is `<tr class="data-row">` with `<a class="jobTitle-link">` for
    title + relative URL, `<span class="jobLocation">` for city + country
    code, `<span class="jobDate">` for listing date ("Aug 9, 2026" format).
  - Job ID is the numeric segment in the URL path: /job/City-Title/1053764966/
  - Location field carries "City, IN" — the fetcher normalises "IN" → "India".

Detail page (description + precise date):
  - `span.jobdescription` holds the full JD text (HTML-rendered, strip tags).
  - `meta[itemprop="datePosted"]` carries "Sun Aug 09 16:00:00 UTC 2026"
    (same anchor as Mastek/Capgemini/Nomura).
  - Apply URL is relative: /talentcommunity/apply/{ID}/?locale=en_GB
    (reconstructed from the job ID).

Pagination: `startrow` is 0-indexed, page size appears to be 20 or the full
result set for the India query. This fetcher simply steps by `num` until the
server returns an empty page, as with all J2W scrapers.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.ltts.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE_DEFAULT = 20  # J2W default; LTTS may cap lower

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from the LTTS J2W tenant."""


# ---------------------------------------------------------------------------
# Date / location helpers
# ---------------------------------------------------------------------------

def _parse_listing_date(raw: str) -> str:
    """'Aug 9, 2026' → '2026-08-09'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """'Sun Aug 09 16:00:00 UTC 2026' → '2026-08-09'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalise_location(raw: str) -> str:
    """'Pune, IN' → 'Pune, India'; 'IN' → 'India'."""
    loc = (raw or "").strip()
    if not loc:
        return "India"
    if loc.upper() == "IN":
        return "India"
    return re.sub(r",\s*IN\b", ", India", loc)


# ---------------------------------------------------------------------------
# Internal page fetcher
# ---------------------------------------------------------------------------

def _fetch_page(keyword: str, start: int, timeout: int) -> str:
    """GET one /search/ page; 3-attempt exponential backoff."""
    params: dict[str, object] = {
        "q": keyword,
        "locationsearch": "india",
    }
    if start:
        params["startrow"] = start

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("LTTS J2W: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"LTTS search fetch failed: {exc}") from exc

    raise RateLimitError(f"LTTS search: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of LTTS India jobs matching *keyword*.

    Keywords are applied server-side (confirmed: nonsense → 0 rows).
    """
    html_text = _fetch_page(keyword, start, timeout)
    soup = BeautifulSoup(html_text, "html.parser")

    jobs: list[dict] = []
    for row in soup.select("tr.data-row"):
        link = row.select_one("a.jobTitle-link")
        if not link:
            continue
        title = html_mod.unescape(link.get_text(strip=True))
        if not title:
            continue

        href = link.get("href", "").strip()
        # href is relative: /job/City-Title/1053764966/
        m = re.search(r"/(\d+)/?$", href)
        job_id = m.group(1) if m else ""
        if not job_id:
            continue

        loc_el = row.select_one("span.jobLocation")
        location_str = _normalise_location(loc_el.get_text(strip=True) if loc_el else "")

        date_el = row.select_one("span.jobDate")
        posting_date = _parse_listing_date(date_el.get_text(strip=True) if date_el else "")

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": f"{_BASE_URL}{href}",
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for one LTTS job detail page.

    Detail page uses `span.jobdescription` for the JD body and
    `meta[itemprop="datePosted"]` for the canonical date (same selectors as
    Mastek/Capgemini/Nomura).
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"LTTS description: 429 for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"LTTS description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"LTTS description: no response — {last_exc}")

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
