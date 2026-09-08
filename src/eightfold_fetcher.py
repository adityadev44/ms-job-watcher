"""Fetches Eightfold.ai's own job listings from its Eightfold Talent Platform tenant.

Eightfold.ai (the AI talent/HR platform vendor) runs its own hiring pipeline on
its own product, at app.eightfold.ai/careers -- the same Eightfold PCSX search
API used by Microsoft (src/fetcher.py) and Morgan Stanley, just with
domain=eightfold.ai instead of domain=microsoft.com. Confirmed live via
DevTools-equivalent direct API probing 2026-09-08:

    GET https://app.eightfold.ai/api/pcsx/search?domain=eightfold.ai&q=...&location=India

returns real, currently-open Bengaluru/Bangalore, Karnataka, India postings
(no PCSX-disabled 403 like HSBC's tenant -- Eightfold's own tenant behaves
like Microsoft/Morgan Stanley's, not HSBC's restricted one).

Keyword search is a no-op: probing with "engineer" vs a nonsense token
("xyzxyznonexistentterm") returned byte-identical position lists in the same
order -- the server ignores `q` entirely and always returns the same
date-sorted pool. Registered in _IGNORES_KEYWORDS so the generic runner only
issues one query pass instead of repeating the configured keyword list.

Pagination via start/num works when a location is supplied (confirmed via
start=0/10/20 returning distinct, non-overlapping ID sets, ending in a
0-result page) -- same shape as Microsoft's tenant, capped around ~10 results
per page with location set.
"""
from __future__ import annotations

import html as html_mod
import re
import time
import warnings
from datetime import datetime, timezone
from typing import Any

import requests

_DOMAIN = "eightfold.ai"
_BASE_URL = "https://app.eightfold.ai"
_SEARCH_URL = f"{_BASE_URL}/api/pcsx/search"
_DETAIL_BASE = f"{_BASE_URL}/api/apply/v2/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class RateLimitError(Exception):
    """Raised when the API returns 429 and all retry attempts are exhausted."""


def _parse_position(raw: dict[str, Any]) -> dict[str, str]:
    job_id = str(raw.get("displayJobId", raw.get("id", "")))
    title = raw.get("name", "") or ""
    locations = raw.get("locations") or []
    location = "; ".join(locations) if locations else ""
    posted_ts = raw.get("postedTs")
    posting_date = (
        datetime.fromtimestamp(posted_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if posted_ts else ""
    )
    position_url = raw.get("positionUrl", "")
    application_url = f"{_BASE_URL}{position_url}?domain={_DOMAIN}" if position_url else ""
    return {
        "id": job_id,
        "title": title,
        "location": location,
        "posting_date": posting_date,
        "application_url": application_url,
    }


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return one page of Eightfold.ai job listings.

    ``keyword`` is accepted for interface compatibility but is a server-side
    no-op for this tenant -- see module docstring.
    """
    params = {
        "domain": _DOMAIN,
        "q": keyword,
        "start": start,
        "num": num,
        "sortBy": sort_by,
    }
    if location:
        params["location"] = location

    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                response = requests.get(
                    _SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout, verify=False
                )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Eightfold request failed after {_MAX_ATTEMPTS} attempts") from exc

        if response.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Eightfold rate-limited after {_MAX_ATTEMPTS} attempts")

        response.raise_for_status()
        raw_data = response.json()
        positions = raw_data.get("data", {}).get("positions") or []
        return [_parse_position(p) for p in positions]

    raise RateLimitError(f"Eightfold rate-limited after {_MAX_ATTEMPTS} attempts")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description (plain text) for a single job."""
    m = re.search(r"/careers/job/(\d+)", application_url)
    if not m:
        return "", ""
    job_id = m.group(1)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        response = requests.get(
            f"{_DETAIL_BASE}/{job_id}",
            headers=_HEADERS,
            params={"domain": _DOMAIN},
            timeout=timeout,
            verify=False,
        )
    response.raise_for_status()
    data = response.json()
    raw_html = data.get("job_description", "") or ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html_mod.unescape(text)
    created = data.get("t_create")
    posting_date = (
        datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
        if created else ""
    )
    return " ".join(text.split()), posting_date
