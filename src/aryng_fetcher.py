"""
Aryng job fetcher — Workable public widget API.

Aryng's own marketing site (`aryng.com/career`) has a static "Current
Openings" section (`<ul id="myTab">...<div id="myTabContent">`) that is
genuinely EMPTY in the server-rendered HTML with no AJAX call backing it
-- confirmed live 2026-09-13 via plain `requests.get`, no job data and no
`fetch()`/XHR call to any job-data endpoint anywhere in the page's own
JS. That page alone would look like a "contact us"-style genuine skip.

However, a live web search for Aryng's actual hiring activity surfaces a
separate, real, working, unauthenticated Workable tenant:
`https://apply.workable.com/aryng/` (confirmed as Aryng's own account —
the widget's `description` field matches Aryng's real company bio
verbatim: founder Piyanka Jain, the "BADIR" framework, etc.). This is the
same public Workable widget API already used by `innovaccer_fetcher.py`:

    GET https://apply.workable.com/api/v1/widget/accounts/aryng

Confirmed live: HTTP 200, valid JSON, `"jobs": []` — Aryng currently has
ZERO open requisitions on this board (a real, verified snapshot, not a
fetch failure: the account and description resolve correctly, only the
job list is empty right now). This mirrors this repo's existing "genuine
current-zero" precedent (see PLAYBOOK.md: Allstate, Resideo, Saxo Bank/
Omnissa for one track) rather than a "no automatable board" skip — the
API itself is real, live, and will surface new India postings the moment
Aryng (a small/boutique data-science consultancy that has historically
posted India-remote Data Scientist roles on this exact tenant, per
external job-aggregator listings) opens a new requisition.

Implementation mirrors `innovaccer_fetcher.py` exactly: the whole board
is fetched once per process and cached (Workable's public widget feed
ignores query params and returns the same full board every time), and
per-job descriptions are fetched from Workable's Markdown export endpoint
(`https://apply.workable.com/aryng/jobs/view/{shortcode}.md`) which
returns the full untruncated posting as plain-ish text needing no HTML
stripping.
"""
from __future__ import annotations

import time

import requests

_ACCOUNT = "aryng"
_WIDGET_URL = f"https://apply.workable.com/api/v1/widget/accounts/{_ACCOUNT}"
_MD_URL_TMPL = f"https://apply.workable.com/{_ACCOUNT}/jobs/view/{{shortcode}}.md"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://aryng.com/career",
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
                raise RateLimitError(f"Aryng {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Aryng {what} failed: {exc}") from exc
    raise RateLimitError(f"Aryng {what}: no response -- {last_exc}")


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
        raise RateLimitError(f"Aryng widget fetch: invalid JSON -- {exc}") from exc

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
    print(f"[Aryng] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Aryng (Workable) postings.

    keyword/location are accepted for interface compatibility but ignored
    -- Workable's public widget feed returns the identical full board
    regardless of query params; the shared matcher does the real
    title/skill/India filtering. The whole board is cached once per
    process (currently empty at the time this fetcher was written -- see
    module docstring).
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
    """Return (description, posting_date) for a single Aryng job.

    Fetched from Workable's Markdown export endpoint, which already
    returns the full, untruncated posting as plain-ish Markdown text --
    no HTML stripping needed.
    """
    shortcode = _shortcode_from_url(application_url)
    if shortcode in _desc_cache:
        description = _desc_cache[shortcode]
    else:
        if not shortcode:
            raise RateLimitError(
                f"Aryng description: could not parse shortcode from {application_url!r}"
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
