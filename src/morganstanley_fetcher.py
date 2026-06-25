"""Fetches Morgan Stanley job listings from the Eightfold PCSX search API.

Morgan Stanley's careers portal is hosted directly on Eightfold AI
(morganstanley.eightfold.ai) — same underlying platform and API shape as
Microsoft's apply.careers.microsoft.com (see fetcher.py), just a different
tenant domain. No Akamai/bot-protection issue observed; plain requests work.
"""

from __future__ import annotations

import time
import warnings
from datetime import datetime, timezone
from typing import Any

import requests


class RateLimitError(Exception):
    """Raised when the API returns 429 and all retry attempts are exhausted."""


_SEARCH_URL = "https://morganstanley.eightfold.ai/api/pcsx/search"
_DETAIL_BASE = "https://morganstanley.eightfold.ai/api/apply/v2/jobs"
_BASE_JOB_URL = "https://morganstanley.eightfold.ai"
_DOMAIN = "morganstanley.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _parse_position(raw: dict[str, Any]) -> dict[str, str]:
    job_id = str(raw.get("displayJobId", raw.get("id", "")))
    title = raw.get("name", "")
    locations = raw.get("locations") or []
    location = "; ".join(locations) if locations else ""
    posted_ts = raw.get("postedTs")
    if posted_ts:
        posting_date = datetime.fromtimestamp(posted_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        posting_date = ""
    position_url = raw.get("positionUrl", "")
    application_url = f"{_BASE_JOB_URL}{position_url}?domain={_DOMAIN}"
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
    sort_by: str = "relevance",
    timeout: int = 20,
) -> list[dict[str, str]]:
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
            raise RateLimitError(f"Request failed after {_MAX_ATTEMPTS} attempts") from exc

        if response.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")

        response.raise_for_status()
        raw_data = response.json()
        positions = raw_data.get("data", {}).get("positions") or []
        return [_parse_position(p) for p in positions]

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")


def _ef_id_from_url(application_url: str) -> str:
    """Extract the numeric Eightfold job ID from the application URL."""
    return application_url.split("/careers/job/")[1].split("?")[0]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description (plain text) for a single job.

    Returns (description_text, posting_date) — posting_date left blank since
    the search results already carry an accurate postedTs.
    """
    import html as html_mod
    import re

    ef_id = _ef_id_from_url(application_url)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = requests.get(
            f"{_DETAIL_BASE}/{ef_id}",
            headers=_HEADERS,
            params={"domain": _DOMAIN},
            timeout=timeout,
            verify=False,
        )
    r.raise_for_status()
    raw_html = r.json().get("job_description", "")
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html_mod.unescape(text)
    return " ".join(text.split()), ""
