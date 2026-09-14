"""
Affine (Affine Analytics) job fetcher — SenseHQ (Skillate white-label) career
site, server-rendered Next.js data.

Affine's own marketing site (`affine.ai/about-us/careers/`) is a client-
rendered Next.js page whose "Open Positions" section never resolves via
plain HTTP (ships only a "Loading open roles…" skeleton) — but it links
out explicitly to "View on SenseHQ": `https://affine.sensehq.com/careers`.
SenseHQ is a white-labeled front end for Skillate (`cookie Domain:
skillate.com`, organization id 222, `com.skillate.web.careers...` class
names throughout the page's own JSON) — a real, distinct-from-anything-
else-in-this-repo ATS vendor.

Unlike a typical client-rendered SPA, SenseHQ's `/careers` page is
server-rendered: the full job pool is embedded directly in the initial
HTML as a Next.js `__NEXT_DATA__` JSON blob at
`props.pageProps.jobsData.rows` — no API token, session, or JS execution
needed. Confirmed live 2026-09-13: HTTP 200, plain `requests.get`, 7 total
open postings (4 India/Bengaluru, 2 US/Wilmington-Delaware, all
`job_status: "OPEN"`). Each row already carries the full HTML job
description inline (`description_external`) — no per-job detail fetch is
strictly required, but `fetch_job_description` still re-fetches the page
(cheap — same 7-row payload) to stay consistent with the shared interface
and to naturally pick up postings added between `fetch_jobs()` calls in a
long-running process.

Location shape: each row has a bare comma-joined city list in `location`
(e.g. "Bengaluru", "Bengaluru,Hyderabad,Gurugram" for one multi-site req)
with no state/country — but `office.country` is reliable and always
present for the row's primary office. `office.country == "India"` is
used to decide whether to append ", India" to the display location (never
blindly). No Chennai/Pune/Kochi/Chandigarh postings were observed live on
this small board; exclude_locations still applies as a safety net if that
changes.

Detail page: `https://affine.sensehq.com/careers/jobs/{id}` — confirmed
200 live. This board's own frontend doesn't expose that path directly in
its HTML anywhere obvious, but it renders correctly and was found by
probing plausible SenseHQ/Skillate URL shapes.

Job IDs are the small integer `id` field. No separate posting-date field
is guaranteed reliable across rows, so `created_on` (a millisecond epoch
timestamp) is converted to YYYY-MM-DD.

Board is small (7 total, 4 India) and not keyword/location filterable
server-side — the whole board is fetched once per process and cached,
same "cache-once" pattern as every Greenhouse-style board in this repo.
require_tech_in_description is NOT enabled — Affine is a direct AI/
analytics consultancy with specific titles ("Data Scientist – Computer
Vision & Machine Learning", "Principal- Senior Data Engineer -
Databricks", "Principal Data Scientist Gen AI") whose bodies name
concrete AI/ML/Python stack terms directly.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime, timezone

import requests

_CAREERS_URL = "https://affine.sensehq.com/careers"
_JOB_URL_TMPL = "https://affine.sensehq.com/careers/jobs/{job_id}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_NEXT_DATA_RE = re.compile(
    r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>', re.S
)

_job_cache: list[dict] = []
_description_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _epoch_ms_to_date(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _fill_cache(timeout: int = 20) -> None:
    """Fetch and parse the SenseHQ careers page once per process."""
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_CAREERS_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Affine: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Affine cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Affine cache fill: no response — {last_exc}")

    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        # Page shape changed / JS-only render — fail soft to empty rather
        # than crash the whole scan cycle.
        print("[Affine] __NEXT_DATA__ block not found — page shape may have changed")
        _job_cache = []
        return

    try:
        data = json.loads(m.group(1))
        rows = data["props"]["pageProps"]["jobsData"]["rows"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"[Affine] Failed to parse jobsData: {exc}")
        _job_cache = []
        return

    collected: list[dict] = []
    for row in rows:
        if (row.get("job_status") or "").upper() != "OPEN":
            continue

        job_id = str(row.get("id") or "")
        title = (row.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc = (row.get("location") or "").strip()
        office = row.get("office") or {}
        if not loc:
            loc = (office.get("city") or "").strip()
        if office.get("country", "").strip().lower() == "india" and "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

        posting_date = _epoch_ms_to_date(row.get("created_on"))

        _description_cache[job_id] = row.get("description_external") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": _JOB_URL_TMPL.format(job_id=job_id),
        })

    _job_cache = collected
    print(f"[Affine] Cache filled: {len(collected)} open jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Affine jobs from the cached SenseHQ board.

    keyword/location are accepted for interface compatibility but ignored
    — the entire small board is server-rendered in one page load with no
    server-side filtering support.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Affine job."""
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").split("/")[-1]
    description = _strip_html(_description_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
