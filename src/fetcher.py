"""Fetches live Microsoft job listings from the Eightfold PCSX search API."""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from typing import Any

import requests

SEARCH_URL = "https://apply.careers.microsoft.com/api/pcsx/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_BASE_JOB_URL = "https://apply.careers.microsoft.com"


def _parse_position(raw: dict[str, Any]) -> dict[str, str]:
    """Convert a single raw API position object into a clean job dict."""
    job_id = str(raw.get("displayJobId", raw.get("id", "")))
    title = raw.get("name", "")
    locations = raw.get("locations") or []
    location = "; ".join(locations) if locations else ""
    posted_ts = raw.get("postedTs")
    if posted_ts:
        posting_date = datetime.fromtimestamp(posted_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
    else:
        posting_date = ""
    position_url = raw.get("positionUrl", "")
    application_url = f"{_BASE_JOB_URL}{position_url}?domain=microsoft.com"
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
    num: int = 10,
    start: int = 0,
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a list of Microsoft job listings matching the given keyword and location.

    Each item in the list has: id, title, location, posting_date, application_url.
    Raises requests.HTTPError if the API returns a non-2xx status.
    """
    params = {
        "domain": "microsoft.com",
        "q": keyword,
        "location": location,
        "start": start,
        "num": num,
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # suppress SSL hostname-mismatch warnings
        response = requests.get(
            SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout, verify=False
        )
    response.raise_for_status()
    raw_data = response.json()
    positions = raw_data.get("data", {}).get("positions") or []
    return [_parse_position(p) for p in positions]
