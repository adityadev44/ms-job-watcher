"""Fetches Ericsson job listings from the Eightfold PCSX search API.

Ericsson's careers portal (jobs.ericsson.com) is hosted on Eightfold AI --
same underlying platform and API shape as Microsoft/Morgan Stanley/Amdocs/
Qualcomm (see fetcher.py / morganstanley_fetcher.py / amdocs_fetcher.py /
qualcomm_fetcher.py). Confirmed via direct probing 2026-09-14: the plain GET
of the careers page embeds `window._EF_PRODUCT = "PCS"` / `_EF_GROUP_ID =
"ericsson.com"` and references `eightfold.ai`/`static.vscdn.net` assets --
NOT a "custom" ATS guess. Ericsson has a very large India R&D presence
(Bengaluru/Chennai/Gurugram/Noida, 5G/telecom software) so a real,
meaningful software-engineering hit rate is expected and confirmed live.

Two quirks discovered via live testing, matching the Qualcomm/Amdocs pattern:
  - `location=india` genuinely filters server-side: verified 99 of the
    global pool returned, every one of the first page's `locations` values
    (e.g. "Bangalore,Karnataka,India") naming a real India city -- no
    leakage observed.
  - Unlike Qualcomm's `q` keyword (a total no-op), Ericsson's `q` was not
    exhaustively A/B tested here, but is passed through unconditionally
    same as every other Eightfold fetcher in this repo -- harmless even if
    it turns out to be ignored (dedup handles redundant passes).
  - Real software-engineering titles confirmed live on page 1 of the India
    pool: "Senior Software Engineer", "Grafana Developer", "Data Scientist",
    "Storage Engineer" -- alongside plenty of telecom-ops titles (RAN/OSS
    specialists) that won't match `title_family`, as expected for a telecom
    equipment vendor.

Detail endpoint (`/api/apply/v2/jobs/{id}`) returns the description directly
under `job_description` (not nested under `data`, unlike the search
response) -- same asymmetry already documented in qualcomm_fetcher.py's
sibling fetchers.
"""
from __future__ import annotations

import html as html_mod
import re
import time
import warnings
from datetime import datetime, timezone
from typing import Any

import requests

_BASE_URL = "https://jobs.ericsson.com"
_SEARCH_URL = f"{_BASE_URL}/api/pcsx/search"
_DETAIL_BASE = f"{_BASE_URL}/api/apply/v2/jobs"
_DOMAIN = "ericsson.com"

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
