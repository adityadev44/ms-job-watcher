"""
Stripe job fetcher — Greenhouse ATS public REST API.

Stripe's careers page (`stripe.com/jobs/search`) embeds a Greenhouse job
board (board token `stripe`); the public boards API is directly reachable
with no auth and no bot wall:

    GET https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true

Confirmed live (2026-09-06): HTTP 200, 616 total postings globally, each
with a full HTML `content` field already inline in the same response — no
separate per-job detail fetch needed (same "cache-once" pattern as
AlphaSense/Databricks/Razorpay/Groww/Paytm, the other Greenhouse boards
already in this repo).

Like every other Greenhouse board here, query params (`content`, or any
attempted `q=`/`search=`) are NOT applied server-side — confirmed by
comparing an unfiltered fetch against one with a nonsense `q=` param: same
616-job payload both times. The full pool is therefore fetched once and
cached in-module (`_cache_filled` is set *before* the fetch attempt, so a
transient failure doesn't retry-storm on every subsequent call in the same
process — the Honeywell lesson, see PLAYBOOK "Key Bugs").

There is no separate posting-date field; `updated_at` (truncated to
YYYY-MM-DD) is used as `posting_date`, per Greenhouse's standard shape.

URL quirk: unlike most Greenhouse boards in this repo (path-style
`/jobs/<id>`), Stripe's `absolute_url` is query-string based
(`https://stripe.com/jobs/search?gh_jid=<id>`) — `_extract_job_id` parses
`gh_jid` explicitly rather than assuming the last path segment is the id.

Location quirk: Stripe's India engineering/ops hub is Bengaluru, but the
board's `location.name` field is wildly inconsistent for the *same* city —
observed live variants: "Bengaluru", "Bengaluru, India", "Bangalore",
"Bangalore " (trailing space), "IN - Bengaluru", "IN-Bengaluru", and
"India, Bangalore " — only 2 of these 7 variants already contain a literal
"india" substring. All are normalised here to `"<city>, India"` (stripping
any existing "IN"/"India" tokens first) so matcher.py's substring-based
`is_india_job()` sees every one of them, following the AlphaSense/Razorpay
convention of appending country text to recognised India city names rather
than trusting the raw string.

Live-verified 2026-09-06 totals: 40 of 616 global postings are India
(all Bengaluru/Bangalore — no other Indian city currently posted). Of
those 40, only 2 pass the standard keyword list ("Software Engineer,
Intern" and "Software Engineer, Internal Systems") — both in the AI/ML/
Python-adjacent "software engineer" track; zero .NET/C# matches (expected
— Stripe's Bengaluru board right now is dominated by Risk/Ops/Finance
roles, e.g. "Fraud Operations Manager", "Credit Risk Operations
Associate", not a fetcher defect). Titles like "Staff Engineer, Core
Infrastructure" and "Staff Engineer, Revenue & Finance Automation" are
real senior-engineering roles but don't literally contain "software
engineer"/"senior software engineer" etc., so they don't trip the shared
keyword list even though a human would call them engineering jobs — same
title-family precision gap already flagged for other companies in
PLAYBOOK.md, not fixed here.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from urllib.parse import parse_qs, urlparse

import requests

_BOARD_TOKEN = "stripe"
_BOARDS_BASE = "https://boards-api.greenhouse.io/v1/boards"
_JOBS_URL = f"{_BOARDS_BASE}/{_BOARD_TOKEN}/jobs"
_FALLBACK_JOB_BASE = f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# India city tokens observed on this board (bare city, no country word) —
# normalised to "<city>, India" below.
_INDIA_CITIES = ("bengaluru", "bangalore")

# Module-level cache: the full board is fetched once and reused for every
# keyword/page call (Greenhouse's public boards API ignores query params).
_job_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _normalize_location(raw_loc: str) -> str:
    """Collapse Stripe's many Bengaluru/Bangalore spellings to '<city>, India'.

    Observed live variants: "Bengaluru", "Bengaluru, India", "Bangalore",
    "Bangalore " (trailing space), "IN - Bengaluru", "IN-Bengaluru",
    "India, Bangalore ". Anything not matching a known India city token is
    returned unchanged (non-India locations are left for matcher.py /
    config's exclude_locations to handle normally).
    """
    loc = (raw_loc or "").strip()
    low = loc.lower()
    if not any(city in low for city in _INDIA_CITIES):
        return loc
    # Strip any existing "IN"/"India" decoration, keep the bare city name.
    cleaned = re.sub(r"\bin\s*-\s*", "", low)
    cleaned = re.sub(r"\bindia\b", "", cleaned)
    cleaned = cleaned.replace(",", " ").strip()
    city = "Bengaluru" if "bengaluru" in cleaned else "Bangalore"
    return f"{city}, India"


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _extract_job_id(application_url: str) -> str:
    """Stripe's Greenhouse `absolute_url` is query-string based
    (`https://stripe.com/jobs/search?gh_jid=<id>`), not the usual
    path-style `/jobs/<id>` — parse `gh_jid` explicitly, falling back to
    the last path segment for any other shape."""
    qs = parse_qs(urlparse(application_url).query)
    if "gh_jid" in qs and qs["gh_jid"]:
        return qs["gh_jid"][0]
    return application_url.rstrip("/").split("/")[-1]


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Stripe Greenhouse board once and cache it.

    _cache_filled is set to True before the fetch attempt so a failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() /
    fetch_job_description() call within the same process (Honeywell
    lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _JOBS_URL,
                headers=_HEADERS,
                params={"content": "true"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Stripe: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Stripe cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Stripe cache fill: no response — {last_exc}")

    raw_jobs = r.json().get("jobs", [])
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc = _normalize_location((j.get("location") or {}).get("name") or "")

        updated_at = j.get("updated_at") or ""
        posting_date = updated_at[:10] if updated_at else ""

        app_url = j.get("absolute_url") or f"{_FALLBACK_JOB_BASE}/{job_id}"

        _content_cache[job_id] = j.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache = collected
    print(f"[Stripe] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Stripe jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full board regardless
    of query params. All jobs (India and non-India) are returned — India
    scoping is left to matcher.py's is_india_job() / config exclude_locations,
    aside from the city-name normalisation documented in the module docstring.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Stripe job.

    Served entirely from the cache filled by _fill_cache() — Greenhouse's
    search response already includes the full HTML `content` field for
    every job, so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = _extract_job_id(application_url)
    description = _strip_html(_content_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
