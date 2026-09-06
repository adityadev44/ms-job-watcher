"""Fetches Innovaccer job listings via the Workable public widget API.

ATS discovery (live, 2026-09-06): innovaccer.com/careers/jobs embeds ~75
`https://apply.workable.com/j/{shortcode}` short-links directly in its raw
HTML (no JS execution needed to see them). Following one short-link resolves
to `https://apply.workable.com/innovaccer-analytics/j/{shortcode}`, which
identifies the Workable account slug as "innovaccer-analytics" (NOT
"innovaccer" -- that slug is unused/404s). Workable exposes a public,
unauthenticated JSON widget feed for any account:

    GET https://apply.workable.com/api/v1/widget/accounts/innovaccer-analytics

Verified live: returns the entire current board (75 jobs, 31 in India) in one
call, no pagination and no server-side keyword/location filtering -- a probe
with `?query=zzznonsensekeyword123` and `?state=india` both returned the
identical 75-job set, confirming query params are ignored server-side (same
"cache-once" family as every other Greenhouse/Lever/Ashby board already in
this repo -- Glean/UiPath/CRED). The whole board is fetched once per process,
cached in-module, and sliced/filtered from there.

Widget list entries do NOT include a description field (only title/location/
department/date metadata) -- a truncation-free per-job description is
fetched from Workable's own Markdown export endpoint:

    GET https://apply.workable.com/innovaccer-analytics/jobs/view/{shortcode}.md

which returns a clean Markdown rendering of the full job page (title,
location/type/posted-date summary line, then the full description body) --
verified live, no HTML stripping needed since it's already plain-ish text;
matcher.py's substring skill-check works fine directly against Markdown
(bold/heading markup doesn't break word-boundary substring matches). This
avoids needing Workable's authenticated detail API entirely.

Fields used: `country`/`city`/`state` (all present and literal for every
posting, e.g. "India"/"Noida"/"Uttar Pradesh" -- no city-whitelist
normalisation hack needed here, unlike Ashby/Lever boards elsewhere in this
repo) and `published_on` (already YYYY-MM-DD).

require_tech_in_description is NOT enabled -- Innovaccer is a direct
healthcare-data-platform employer with specific engineering titles ("Staff
Engineer - Healthcare Interoperability (Gravity)", "Vice President,
Implementation Engineering") rather than generic IT-services level bands.
"""
from __future__ import annotations

import re
import time

import requests

_ACCOUNT = "innovaccer-analytics"
_WIDGET_URL = f"https://apply.workable.com/api/v1/widget/accounts/{_ACCOUNT}"
_MD_URL_TMPL = f"https://apply.workable.com/{_ACCOUNT}/jobs/view/{{shortcode}}.md"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://innovaccer.com/careers/jobs",
}

# Module-level cache: the widget feed returns the identical full board for
# every query -- fetch it once and slice/filter after.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _get(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Innovaccer {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Innovaccer {what} failed: {exc}") from exc
    raise RateLimitError(f"Innovaccer {what}: no response -- {last_exc}")


def _parse_date(raw: str) -> str:
    return (raw or "")[:10]


def _location_from_job(job: dict) -> str:
    parts = [
        (job.get("city") or "").strip(),
        (job.get("state") or "").strip(),
        (job.get("country") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Workable board once; cache job list.

    _cache_filled is set before the request attempt so a transient failure
    doesn't retry-storm on every subsequent fetch_jobs() call in the same
    process run (Honeywell/Persistent lesson -- see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = _get(_WIDGET_URL, timeout, "widget fetch")
    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Innovaccer widget fetch: invalid JSON -- {exc}") from exc

    raw_jobs = payload.get("jobs") or []
    collected: list[dict] = []
    for j in raw_jobs:
        shortcode = (j.get("shortcode") or "").strip()
        title = (j.get("title") or "").strip()
        if not (shortcode and title):
            continue
        collected.append({
            "id": shortcode,
            "title": title,
            "location": _location_from_job(j) or "",
            "posting_date": _parse_date(j.get("published_on") or j.get("created_at") or ""),
            "application_url": j.get("url") or f"https://apply.workable.com/j/{shortcode}",
        })

    _job_cache[:] = collected
    print(f"[Innovaccer] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Innovaccer (Workable) postings.

    keyword/location are accepted for interface compatibility but ignored --
    a live probe confirmed Workable's public widget feed returns the
    identical full board regardless of query params; the shared matcher
    does the real title/skill/India filtering. The whole board is cached
    once per process.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start: start + num]


def _shortcode_from_url(application_url: str) -> str:
    """application_url is '.../j/{shortcode}' (optionally trailing '/apply')."""
    path = (application_url or "").split("?", 1)[0].rstrip("/")
    if path.endswith("/apply"):
        path = path[: -len("/apply")]
    return path.rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Innovaccer job.

    Fetched from Workable's Markdown export endpoint, which already returns
    the full, untruncated posting as plain-ish Markdown text -- no HTML
    stripping needed.
    """
    shortcode = _shortcode_from_url(application_url)
    if shortcode in _desc_cache:
        description = _desc_cache[shortcode]
    else:
        if not shortcode:
            raise RateLimitError(
                f"Innovaccer description: could not parse shortcode from {application_url!r}"
            )
        r = _get(_MD_URL_TMPL.format(shortcode=shortcode), timeout, "description fetch")
        description = " ".join((r.text or "").split())
        _desc_cache[shortcode] = description

    posting_date = ""
    for job in _job_cache:
        if job["id"] == shortcode:
            posting_date = job["posting_date"]
            break

    return description, posting_date
