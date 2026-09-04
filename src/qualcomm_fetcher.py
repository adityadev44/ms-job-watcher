"""Fetches Qualcomm job listings from the Eightfold PCSX search API.

Qualcomm's careers portal (careers.qualcomm.com) is hosted on Eightfold AI --
same underlying platform and API shape as Microsoft/Morgan Stanley/Amdocs
(see fetcher.py / morganstanley_fetcher.py / amdocs_fetcher.py). Confirmed
via live DevTools-equivalent verification (`x-ef-*` response headers,
`static.vscdn.net` asset CDN, `pcsx_view` trace namespace on a plain GET of
the careers page) -- NOT verified from a prior "custom ATS" guess, which per
the playbook's Wave 5/6 lesson was wrong 11 of 14 times on other GCCs. Both
`careers.qualcomm.com/api/pcsx/search` and the dedicated `qualcomm.eightfold.ai`
tenant domain return identical results; the branded domain is used here to
match the visible apply-flow host.

Two quirks discovered via live testing, matching the Amdocs pattern exactly:
  - The `q` keyword parameter is genuinely ignored server-side -- wildly
    unrelated keywords ("engineer", "marketing", "facilities", "sourcing",
    ".net", "python") all returned the identical `count` (565 for a
    location=India query) and same ordered result set. Every keyword pass
    therefore returns the same pool; deduplication in matcher.py handles
    the redundancy.
  - The `location` parameter DOES genuinely filter server-side (confirmed:
    location=India query returns exactly 565 of the unfiltered ~1961-job
    global pool, and every one of the 565 sampled `locations` values names
    a real India city -- Bangalore, Hyderabad, Chennai, Noida, Pune, Delhi,
    Mumbai -- no leakage to other countries observed, unlike Micron/Verizon/
    Lowe's broken Workday facets).
  - Page size caps at 10 once `location` is set (requesting num=100 still
    returns 10 positions per page), same "location narrows to distance sort
    and a smaller page" behavior noted in fetcher.py's Microsoft docstring.
    sortBy is silently overridden to "distance" once a location is applied,
    so results are not reliably newest-first with location set -- caller
    (matcher.py) does not depend on ordering.
"""
from __future__ import annotations

import html as html_mod
import re
import time
import warnings
from datetime import datetime, timezone
from typing import Any

import requests

_BASE_URL = "https://careers.qualcomm.com"
_SEARCH_URL = f"{_BASE_URL}/api/pcsx/search"
_DETAIL_BASE = f"{_BASE_URL}/api/apply/v2/jobs"
_DOMAIN = "qualcomm.com"

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
    title = raw.get("name", "")
    locations = raw.get("locations") or []
    location = "; ".join(locations) if locations else ""
    posted_ts = raw.get("postedTs")
    if posted_ts:
        posting_date = datetime.fromtimestamp(posted_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        posting_date = ""
    position_url = raw.get("positionUrl", "")
    application_url = f"{_BASE_URL}{position_url}?domain={_DOMAIN}"
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

    Returns (description_text, posting_date) -- posting_date left blank
    since the search results already carry an accurate postedTs.
    """
    ef_id = _ef_id_from_url(application_url)

    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    f"{_DETAIL_BASE}/{ef_id}",
                    headers=_HEADERS,
                    params={"domain": _DOMAIN},
                    timeout=timeout,
                    verify=False,
                )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Request failed after {_MAX_ATTEMPTS} attempts") from exc

        if r.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")

        r.raise_for_status()
        raw_html = r.json().get("job_description", "") or ""
        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = html_mod.unescape(text)
        return " ".join(text.split()), ""

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")
