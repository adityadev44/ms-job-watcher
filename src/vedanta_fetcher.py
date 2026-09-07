"""Fetches Vedanta Limited job listings — Darwinbox candidate portal,
tenant "vhr", running the older "candidate" SPA (same product tier as
Zomato/Eternal and Sonata Software's Darwinbox tenants in this repo, NOT
the newer "candidatev2" SPA used by Perfios/Signzy/Darwinbox's own board).

ATS discovery (live, 2026-09-07): vedantalimited.com/eng/work-at-vedanta.php
links directly to ``https://vhr.darwinbox.in/ms/candidate/careers`` — no
redirect chain or JS indirection needed to find it, unlike some other
tenants in this repo.

Unlike sibling tenant ``sonataone.darwinbox.in`` (Cloudflare-gated, requires
Playwright), this tenant answers plain ``requests.get()`` directly —
confirmed live (2026-09-07): a cold, header-only GET to the API returns
real JSON with no bot-management challenge, same as Zomato/Eternal's
"eternal" tenant. No Playwright needed here.

API endpoints (same shape as zomato_fetcher.py/sonatasoftware_fetcher.py —
this is a shared Darwinbox "candidate" SPA product, not tenant-specific):

    GET /ms/candidateapi/job?page=N&limit=M
        → {"status":"success","message":{"jobscount":N,"jobs":[...]}}
    GET /ms/candidateapi/job/{id}
        → {"status":"success","message":{"job":[{"id":...,"jd":...,
            "posted_on":...}]}}

Server-side filtering — tested live against the real API:
  - ``keyword`` is IGNORED: a nonsense token
    (``zzznonsensequeryabc123``) returns the same ``jobscount: 13`` as no
    keyword at all. Same finding as every other "candidate"-SPA Darwinbox
    tenant in this repo (Zomato/Sonata/etc.) — the whole (tiny) board is
    cached once per process and matcher.py's own title/skill filters do
    the real narrowing.
  - ``location=India`` collapses 13 jobs to 0 (the API evidently expects a
    numeric location ID, not a free-text value) — never sent, same lesson
    as Sonata/Zomato: a guessed param value silently zeroes real results.

Current live board (2026-09-07): only 13 total postings, split between
Indian mining/metals sites (Jharsuguda/Korba/Bokaro/Panaji/Bhadrak — all
Vedanta Limited's own Indian operations) and Vedanta Zinc International's
South African site (Aggeneys, Northern Cape) under the same tenant. None
of the 13 are software/tech roles today (Mining Ops, Sales & Accounting,
Finance, HR, Safety, Commercial) — a genuine "zero is a fact" result for
this specific portal right now (same precedent as Darwinbox's own board
elsewhere in this repo), not a fetcher bug. The pipeline is mechanically
correct and will surface a real match automatically the moment Vedanta
posts a matching title here.

Location handling: ``officelocation_show_arr`` is already a clean
"City, State, India" string for Indian postings (no normalization
needed, same as Sonata) and a genuine "Aggeneys, Northern Cape, South
Africa" for the one overseas site — ``is_india_job()`` works on both as-is.
One posting ("Head Exploration - FACOR") uses the "Multiple locations"
placeholder with two real India cities in ``tool_tip_locations``; those are
joined the same way as Zomato's identical handling.

Application URL: ``https://vhr.darwinbox.in/ms/candidate/careers/job/{id}``
(same convention as the other "candidate"-SPA tenants in this repo).

Descriptions: NOT inline in the list response (confirmed live — the list
JSON has no ``jd`` key) — a separate ``GET job/{id}`` call is required and
cached by job id, same as Zomato/Sonata.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timezone

import requests

_TENANT = "vhr"
_BASE = f"https://{_TENANT}.darwinbox.in"
_API_BASE = f"{_BASE}/ms/candidateapi/"
_JOB_LIST_URL = f"{_API_BASE}job"
_JOB_DETAIL_BASE = f"{_API_BASE}job/"
_CAREERS_PAGE = f"{_BASE}/ms/candidate/careers"
_JOB_PAGE_BASE = f"{_CAREERS_PAGE}/job/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
    "Referer": _CAREERS_PAGE,
}

_PAGE_SIZE = 50
_MAX_PAGES = 40  # safety cap (~2000 jobs) well beyond this ~13-job board

# Module-level cache: keyword/location params are ignored/unreliable
# server-side (verified live), so the whole (tiny) board is paginated
# through once per process and sliced/looked-up after.
_job_cache: list[dict] = []
_detail_cache: dict[str, dict] = {}
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from Vedanta's Darwinbox tenant."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _epoch_to_date(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def _location_from_job(job: dict) -> str:
    """Prefer the display location; fall back to the tooltip list for
    "Multiple locations" placeholders (same logic as zomato_fetcher.py)."""
    loc = (job.get("officelocation_show_arr") or "").strip()
    if loc and loc.lower() != "multiple locations":
        return loc
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    return loc or "India"


def _get_json(url: str, *, params: dict | None = None, timeout: int = 20, context: str = "") -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Vedanta {context}: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Vedanta {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Vedanta {context}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through the entire Darwinbox board once and cache it.

    ``_cache_filled`` is set before the loop so a mid-fetch failure doesn't
    trigger a retry storm on every subsequent keyword call.
    """
    global _cache_filled, _FIRST_PAGE_IDS, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None

    for page in range(1, _MAX_PAGES + 1):
        try:
            data = _get_json(
                _JOB_LIST_URL,
                params={"page": page, "limit": _PAGE_SIZE},
                timeout=timeout,
                context=f"job list page {page}",
            )
        except RateLimitError as exc:
            _cache_error = exc
            raise

        raw_jobs = ((data or {}).get("message") or {}).get("jobs") or []
        if not raw_jobs:
            break

        page_ids: set[str] = set()
        new_this_page = 0
        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or "").strip()
            if not (job_id and title):
                continue
            page_ids.add(job_id)
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            new_this_page += 1
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": _epoch_to_date(j.get("job_posting_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}",
            })

        if page == 1:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1

        if new_this_page == 0:
            break

    _job_cache[:] = collected
    print(f"[Vedanta] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Vedanta (Darwinbox "vhr" tenant) postings.

    keyword/location are accepted for interface compatibility but ignored
    server-side (verified live: a nonsense keyword returns the same count
    as no keyword, and a real location value zeroes results); the shared
    matcher does the real title/skill/India filtering. The whole (tiny)
    board is paginated through and cached once per process.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    return application_url.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Vedanta job.

    Descriptions are NOT included in the list response — a separate
    detail call (`GET job/{id}`) is required and cached by job id.
    """
    job_id = _job_id_from_url(application_url)

    detail = _detail_cache.get(job_id)
    if detail is None:
        data = _get_json(
            f"{_JOB_DETAIL_BASE}{job_id}",
            timeout=timeout,
            context=f"job detail {job_id}",
        )
        job_list = ((data or {}).get("message") or {}).get("job") or []
        detail = job_list[0] if job_list else {}
        _detail_cache[job_id] = detail

    description = _strip_html(detail.get("jd") or "")
    posting_date = _epoch_to_date(detail.get("posted_on") or detail.get("job_posting_on"))
    return description, posting_date
