"""Fetches CRED job listings via the Lever ATS public REST API.

CRED's careers site (careers.cred.club) is backed by Lever, token "cred".
GET https://api.lever.co/v0/postings/cred?mode=json returns the entire
board in one call — verified live 2026-08-30, ~14 postings. No server-side
keyword/location filtering, no pagination, and no separate detail endpoint
exists (the search response already carries everything).

Description: `descriptionPlain` is only the intro paragraph — the actual
skill-bearing content ("what you will do" / "you should apply if") lives in
the `lists` array as HTML with no plain-text sibling field. Concatenating
descriptionPlain + HTML-stripped `lists[].content` (+ `additionalPlain`)
into one description is still zero extra HTTP calls (same response), but
matters a lot for matcher.py's skill check — using descriptionPlain alone
would silently miss most of the hard skill terms, which sit in bullets.

Company scope: the board also lists roles for CRED's lending subsidiary
Prefr/CreditVidya (categories.department == "Prefr") interleaved with
CRED-proper roles (department "product & growth", "business", etc.) — kept
as-is; no legal-entity filtering, same as every other fetcher in this repo.

Location: Lever's own `categories.location`/`allLocations` values are bare
city/state names ("bengaluru", "tamil nadu", "mumbai") — never containing
"india" as a substring, so returning them unmodified would make matcher.py's
substring-based is_india_job() drop every single posting (unlike WTW, where
real India locations already say "India"). Every observed posting instead
carries a top-level `country: "IN"` field, which is a clean, authoritative
per-job signal — used here to append ", India" to the location string.
Postings where country != "IN" (CRED has none today, but the board could
change) are left unmodified so is_india_job() naturally excludes them —
same defense-in-depth spirit as Lowe's city whitelist, keyed off a real
field instead of a guessed one. Pre-filtering is done in the sense of
normalizing location text for is_india_job()/exclude_locations to work
correctly; no jobs are dropped in the fetcher itself — matcher.py still
makes the final call, so a non-IN posting is skipped there rather than here.

Caching: the whole board is fetched and cached once per process (module-
level cache; `_cache_filled = True` is set *before* the request attempt, so
a failed fetch doesn't retry on every subsequent fetch_jobs() call in the
same run — the Honeywell/Persistent "avoid a retry storm" lesson). Keyword/
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

_BOARD_URL = "https://api.lever.co/v0/postings/cred?mode=json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.cred.club/",
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

    descriptionPlain alone is just the intro blurb; the responsibilities/
    requirements bullets (where hard skill terms actually appear) live only
    as HTML in `lists`. All from the one cached search response.
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
                raise RateLimitError("CRED Lever board: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"CRED board fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"CRED board fetch: no response — {last_exc}")

    try:
        postings = r.json()
    except ValueError as exc:
        raise RateLimitError(f"CRED board fetch: invalid JSON — {exc}") from exc

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
    print(f"[CRED] Cache filled: {len(jobs)} jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of CRED (Lever) postings.

    Keyword/location are accepted but ignored — Lever's public postings
    endpoint returns the identical full board regardless of query params;
    the shared matcher does the real title/skill filtering. The whole board
    is cached once and sliced here.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    """hostedUrl is typically .../cred/{id}[?query]; take the last segment."""
    path = (application_url or "").split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single CRED job.

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
