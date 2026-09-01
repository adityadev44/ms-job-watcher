"""Fetches Zeta job listings via the Lever ATS public REST API.

Zeta (zeta.tech — "Build the future of banking", cloud-native core banking
platform for issuers) is backed by Lever, board token "zeta". Confirmed live
2026-08-31 via GET https://api.lever.co/v0/postings/zeta?mode=json — 20
postings, real Bangalore/Mumbai India office locations, `country: "IN"` on
every posting. Identical API shape to this repo's existing CRED/Meesho/Paytm
Lever fetchers: no server-side keyword/location filtering, no pagination, no
separate detail endpoint (the search response already carries the full JD).

Description: same as CRED — `descriptionPlain` alone is only the intro
paragraph; the responsibilities/skills/qualifications bullets (where the
hard skill terms actually live) are HTML in the `lists` array with no plain-
text sibling field. Concatenated here, same as `cred_fetcher.py`.

Location: Lever's `categories.location`/`allLocations` values are bare city
names ("Bangalore", "Mumbai", "Mumbai - Direct Sales") with no "india"
substring — matcher.py's is_india_job() would drop every posting if these
were passed through unmodified. Every observed posting carries a top-level
`country: "IN"` field (same authoritative signal CRED/Meesho/Paytm already
use), used here to append ", India" to the location string.

Caching: the whole board is fetched and cached once per process
(`_cache_filled = True` set before the request attempt — the Honeywell/
Persistent "avoid a retry storm on transient failure" lesson). Keyword/
location args are accepted but ignored; fetch_jobs() just slices the cache.
fetch_job_description() is served entirely from the cache, keyed by job ID
parsed from the tail of hostedUrl/application_url.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timezone

import requests

_BOARD_URL = "https://api.lever.co/v0/postings/zeta?mode=json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://jobs.lever.co/zeta",
}

# Module-level cache: the Lever board endpoint returns the same full list
# for every query, so fetch it once and slice/look up afterward.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(created_at_ms) -> str:
    """Convert Lever's epoch-millisecond createdAt to YYYY-MM-DD."""
    if not created_at_ms:
        return ""
    try:
        return datetime.fromtimestamp(
            int(created_at_ms) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def _build_description(posting: dict) -> str:
    """Concatenate descriptionPlain + stripped `lists` sections.

    descriptionPlain alone is just the company-intro blurb; the
    responsibilities/skills/qualifications bullets (where hard skill terms
    actually appear) live only as HTML in `lists`. All from the one cached
    search response — no extra HTTP call.
    """
    parts = [posting.get("descriptionPlain") or ""]
    for section in posting.get("lists") or []:
        heading = (section.get("text") or "").strip()
        body = _strip_html(section.get("content") or "")
        if body:
            parts.append(f"{heading} {body}".strip() if heading else body)
    closing = _strip_html(posting.get("additionalPlain") or posting.get("additional") or "")
    if closing:
        parts.append(closing)
    return " ".join(p.strip() for p in parts if p and p.strip())


def _location_from_posting(posting: dict) -> str:
    cats = posting.get("categories") or {}
    raw_loc = (cats.get("location") or "").strip()
    loc_title = ", ".join(
        part.strip().title() for part in raw_loc.split(",") if part.strip()
    ) or "India"
    is_india = (posting.get("country") or "").strip().upper() == "IN"
    if is_india and "india" not in loc_title.lower():
        return f"{loc_title}, India"
    return loc_title


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Lever board once; cache job list + descriptions.

    _cache_filled is set before the request attempt so a transient failure
    doesn't trigger a retry storm on every fetch_jobs() call made during the
    same process run (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_BOARD_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Zeta Lever board: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zeta board fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Zeta board fetch: no response — {last_exc}")

    try:
        postings = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Zeta board fetch: invalid JSON — {exc}") from exc

    if not isinstance(postings, list):
        return

    jobs: list[dict] = []
    for p in postings:
        job_id = str(p.get("id") or "").strip()
        title = (p.get("text") or "").strip()
        hosted_url = p.get("hostedUrl") or p.get("applyUrl") or ""
        if not (job_id and title and hosted_url):
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": _location_from_posting(p),
            "posting_date": _parse_date(p.get("createdAt")),
            "application_url": hosted_url,
        })
        _desc_cache[job_id] = _build_description(p)

    _job_cache[:] = jobs
    print(f"[Zeta] Cache filled: {len(jobs)} jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Zeta (Lever) postings.

    Keyword/location are accepted but ignored — Lever's public postings
    endpoint returns the identical full board regardless of query params;
    the shared matcher does the real title/skill filtering. The whole board
    is cached once and sliced here.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    """hostedUrl is typically .../zeta/{id}[?query]; take the last segment."""
    path = (application_url or "").split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Zeta job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    Lever's search response already carries the full description, so there
    is no separate detail endpoint to call.
    """
    _fill_cache(timeout=timeout)
    job_id = _job_id_from_url(application_url)
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
