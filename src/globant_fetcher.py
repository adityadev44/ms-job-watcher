"""Fetches Globant (career.globant.com) India job listings via the
internal SAP SuccessFactors proxy API served by their Next.js career site.

Globant's main website (globant.com/careers) blocks unauthenticated access
(403), but their dedicated career portal at career.globant.com exposes an
unauthenticated internal API at /api/sap/job-requisition-v1 that wraps their
SAP SuccessFactors tenant and returns full job data without authentication.

Key facts confirmed live 2026-09-06:
  - POST /api/sap/job-requisition-v1 with JSON body {"country": ["IN"]}
    returns India jobs only; country must be an array of ISO-2 codes.
  - 15 India jobs total (2 pages of 10/5); pagination via `page: N`
    (1-indexed). `showMore: true` indicates more pages.
  - Keywords are NOT applied server-side (both "python" and a nonsense
    string return the same 15 India jobs) — the keyword argument is ignored;
    all India jobs are cached once per process run.
  - Each job has: `jobReqId` (numeric ID), `jobTitle`, `location` ("City,
    Country" string), `country` ("India"), `createdDateTime` (ISO 8601),
    `jobDescription` (HTML, the full inline JD).
  - Canonical job URL: https://career.globant.com/?id={jobReqId}
  - Descriptions are inline in the job-requisition response; the coordinator
    should add "globant" to _INLINE_DESCRIPTIONS in the registry.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_API_URL = "https://career.globant.com/api/sap/job-requisition-v1"
_JOB_BASE_URL = "https://career.globant.com/?id="
_PAGE_SIZE = 10

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://career.globant.com/",
    "Origin": "https://career.globant.com",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from Globant's SAP proxy."""


# Module-level cache: keywords are not applied server-side, so the full India
# pool is fetched once and served from cache for all keyword calls.
_job_cache: list[dict] = []
_cache_filled: bool = False


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _post_page(page: int, timeout: int) -> dict:
    """POST one page; 3-attempt exponential backoff."""
    payload = {"country": ["IN"], "page": page}
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(
                _API_URL, json=payload, headers=_HEADERS, timeout=timeout
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Globant: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Globant fetch failed: {exc}") from exc

    raise RateLimitError(f"Globant: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch all India jobs across pages and cache them."""
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    page = 1

    while True:
        data = _post_page(page, timeout)
        items = data.get("jobRequisition") or []
        for item in items:
            job_id = str(item.get("jobReqId", "")).strip()
            title = (item.get("jobTitle") or "").strip()
            if not (job_id and title):
                continue
            if job_id in seen_ids:
                continue

            location_raw = (item.get("location") or "").strip()
            # Normalize: "Maharashtra, India" → keep as-is; ensure "India"
            location = location_raw if location_raw else "India"
            if "india" not in location.lower():
                location = f"{location}, India"

            # Date: "2026-07-01T06:38:39.000Z" → "2026-07-01"
            created = (item.get("createdDateTime") or "")[:10]

            # Description is inline HTML
            desc_html = (item.get("jobDescription") or "")
            description = _strip_html(desc_html)

            seen_ids.add(job_id)
            jobs.append({
                "id": job_id,
                "title": title,
                "location": location,
                "posting_date": created,
                "description": description,
                "application_url": f"{_JOB_BASE_URL}{job_id}",
            })

        if not data.get("showMore", False):
            break
        page += 1
        time.sleep(0.2)

    _job_cache[:] = jobs
    print(f"[Globant] Cache filled: {len(jobs)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of Globant India jobs.

    Keywords are accepted but not applied server-side; the full India pool is
    cached once per process run.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Globant descriptions are returned inline by fetch_jobs."""
    raise NotImplementedError("descriptions are inline")
