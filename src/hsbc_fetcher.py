"""Fetches HSBC job listings via the Eightfold "related jobs" widget API.

HSBC migrated off its old Avature portal (mycareer.hsbc.com) to Eightfold
(portal.careers.hsbc.com) -- the old URL now shows a banner linking to the
new site. Same underlying ATS as Microsoft/Morgan Stanley (see fetcher.py),
but HSBC's tenant has PCSX (the normal public search API used by those two)
disabled: GET /api/pcsx/search returns 403 "PCSX is not enabled for this
user" both on portal.careers.hsbc.com and hsbc.eightfold.ai directly.

The only working avenue, found via Playwright network capture on
https://portal.careers.hsbc.com/careers/jobs?query=...&location=..., is the
"related/similar jobs" widget endpoint:

    GET /api/apply/v2/jobs/{ANCHOR_ID}/jobs?domain=hsbc.com&query=...&location=...

{ANCHOR_ID} is a job ID the widget uses as a similarity seed -- an arbitrary
fake ID returns zero results, so a real, currently-open job ID is required.
563774609963508 was observed live and hardcoded below (same pattern as Wells
Fargo's hardcoded India WID or Maersk's location WIDs -- a discovered magic
constant, not a config value). If HSBC ever closes that specific req, this
constant will need refreshing to another real job ID (grab one from
https://portal.careers.hsbc.com/careers/jobs -- any `/api/apply/v2/jobs/{id}`
call in the network tab works as the new anchor).

KNOWN LIMITATION: this endpoint hard-caps at 10 results and ignores
start/num/offset/page entirely -- there is no working pagination. Six
keywords x 10 results each (deduped) is the practical ceiling per run. This
is a real widget for the site's "explore similar roles" feature, not a full
search API, so it's inherently narrower than other pipelines' coverage.
"""
from __future__ import annotations

import html as html_mod
import re
import time
import warnings
from datetime import datetime, timezone
from typing import Any

import requests

_BASE_URL = "https://portal.careers.hsbc.com"
_ANCHOR_ID = 563774609963508
_SEARCH_URL = f"{_BASE_URL}/api/apply/v2/jobs/{_ANCHOR_ID}/jobs"
_DETAIL_BASE = f"{_BASE_URL}/api/apply/v2/jobs"
_DOMAIN = "hsbc.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from HSBC's Eightfold tenant."""


def _parse_position(raw: dict[str, Any]) -> dict[str, str]:
    job_id = str(raw.get("display_job_id") or raw.get("id", ""))
    title = raw.get("name") or raw.get("posting_name") or ""
    locations = raw.get("locations") or []
    location = "; ".join(locations) if locations else (raw.get("location") or "")
    created = raw.get("t_create")
    posting_date = (
        datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
        if created else ""
    )
    application_url = raw.get("canonicalPositionUrl") or f"{_BASE_URL}/careers/job/{raw.get('id', '')}"
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
    sort_by: str = "relevance",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return HSBC job listings for one keyword/location combination.

    Hard-capped at 10 results by the underlying widget endpoint -- start>0
    always returns the same 10, so callers should not paginate past start=0.
    """
    if start > 0:
        return []

    params = {"domain": _DOMAIN, "query": keyword}
    if location:
        params["location"] = location

    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout, verify=False)
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"HSBC request failed after {_MAX_ATTEMPTS} attempts") from exc

        if r.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"HSBC rate-limited after {_MAX_ATTEMPTS} attempts")

        r.raise_for_status()
        positions = r.json().get("positions") or []
        return [_parse_position(p) for p in positions]

    raise RateLimitError(f"HSBC rate-limited after {_MAX_ATTEMPTS} attempts")


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description (plain text) for a single job."""
    m = re.search(r"/job/(\d+)", application_url)
    if not m:
        return "", ""
    job_id = m.group(1)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = requests.get(
            f"{_DETAIL_BASE}/{job_id}",
            headers=_HEADERS,
            params={"domain": _DOMAIN},
            timeout=timeout,
            verify=False,
        )
    r.raise_for_status()
    data = r.json()
    raw_html = data.get("job_description", "") or ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html_mod.unescape(text)
    created = data.get("t_create")
    posting_date = (
        datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
        if created else ""
    )
    return " ".join(text.split()), posting_date
