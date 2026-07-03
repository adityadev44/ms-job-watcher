"""Fetches Societe Generale job listings via careers.societegenerale.com.

The site's own client-side search (search-proxy.php, an Exalead/CloudView
enterprise search backend) requires a bearer token from a `get-token`
endpoint that returns 403 to anything but the page's own first-load request
— replaying it manually (even from within the same Playwright page's JS
context via `fetch()`) still gets 403, so it's not a viable API path.

Instead: the "all-job-offers" listing page is fully server-rendered — a
plain GET returns all ~694 postings embedded directly in the HTML (each in
a `<div data-offer-id="...">` block with title/location), no JS or token
needed. Single request, no pagination. Job-detail pages carry a clean
schema.org JobPosting JSON-LD block with plain-text `description` and
`datePosted` — no HTML stripping required.
"""
from __future__ import annotations

import json
import re
import time

import requests
from bs4 import BeautifulSoup

_BASE = "https://careers.societegenerale.com"
_LIST_URL = f"{_BASE}/en/Technical/all-job-offers"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

# Module-level cache: filled once, reused for all keyword calls.
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _fill_cache(timeout: int = 30) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_LIST_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Societe Generale: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Societe Generale cache fill failed after 3 attempts: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"Societe Generale cache fill: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")
    collected: list[dict] = []
    for offer in soup.select("div[data-offer-id]"):
        job_id = offer.get("data-offer-id", "")
        link = offer.find("a", href=True)
        if not (job_id and link):
            continue
        title = link.get_text(strip=True)
        loc_span = offer.select_one(".nobreak")
        location = loc_span.get_text(strip=True) if loc_span else ""
        if "india" not in location.lower():
            continue

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",  # filled in on description fetch (JSON-LD datePosted)
            "application_url": link["href"],
        })

    _india_cache = collected
    print(f"[Societe Generale] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Societe Generale India jobs.

    The listing page has no server-side keyword search — all India jobs are
    cached in one pass and re-served from cache for every keyword call.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


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
                raise RateLimitError("Societe Generale description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Societe Generale description fetch failed: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"Societe Generale description fetch: no response — {last_exc}")

    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.DOTALL)
    if not m:
        return "", ""

    try:
        data = json.loads(m.group(1))
    except ValueError:
        return "", ""

    description = data.get("description", "") or ""
    posted = (data.get("datePosted", "") or "").replace("/", "-")
    return description, posted
