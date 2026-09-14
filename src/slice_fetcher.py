"""slice (sliceit.com / slice.bank.in) job fetcher — Kula ATS.

ATS discovery (2026-09-14): slice.bank.in/careers/open-positions (the
sliceit.com domain 301s here) embeds an iframe:

    <iframe id="kula_embed" src="https://careers.kula.ai/slice?jobs=true">

"Kula" is a new ATS vendor for this repo (not previously documented in
PLAYBOOK.md). The embedded page is a Next.js app whose own client JS calls
a clean, unauthenticated first-party JSON API (found via Playwright network
capture — no plain-`requests` API string was visible in any downloaded JS
chunk, so this required watching real XHR traffic rather than reading
bundles):

    GET https://careers.kula.ai/api/internal/ats_job_posts
            ?accountName=slice&page=N&type=ats_job_post.index&items=100
        -> {"data": [...], "meta": {"count", "page", "items", "pages"}, "errors": [...]}

Confirmed live with plain `requests.get()` (no cookies/auth/Cloudflare
gating at all — a pleasant surprise after several other Indian fintechs in
this repo needed Playwright for their Darwinbox tenants). `items` is capped
at 100 server-side regardless of what's requested; pagination walks `page`
until it exceeds `meta["pages"]`.

Each item's shape (verified against the live "slice" tenant, 38 jobs):
    id                          : int job ID
    title                       : posting title
    launch_at                   : ISO8601 posting timestamp
    ats_job.job_description     : full HTML description, INLINE — no
                                   separate detail call needed
    ats_job.ats_department.name : department (not used directly; title/skill
                                   filtering is matcher.py's job)
    ats_job.offices[]           : list of {location, city, state, country,
                                   remote}; "location" is already a full
                                   "City, State, India" (or bare "India" for
                                   remote-anywhere postings) string — no
                                   city-name normalisation needed, unlike
                                   Razorpay's bare-city Greenhouse board.
                                   Multiple offices are joined with "; "
                                   (none observed live, but Uniphore/Sonata
                                   precedent shows this can happen).

No server-side keyword/department filtering is exercised by this fetcher —
the whole (small) board is cached once per process and matcher.py's
title/skill layers do the real narrowing, same "ignores keywords" pattern
as most other Indian-fintech Darwinbox/Greenhouse boards in this repo.

Application URL: ``https://careers.kula.ai/slice/{id}`` (confirmed to
render the real job detail page without the `?jobs=true` embed-mode query
param).
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_TENANT = "slice"
_API_URL = "https://careers.kula.ai/api/internal/ats_job_posts"
_JOB_PAGE_BASE = f"https://careers.kula.ai/{_TENANT}/"

_PAGE_SIZE = 100
_MAX_PAGES = 20  # safety cap (~2,000 jobs), well beyond this board's size

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


_job_cache: list[dict] = []
_description_cache: dict[str, str] = {}
_cache_filled: bool = False


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _location_from_offices(offices: list) -> str:
    locs = [
        (o.get("location") or "").strip()
        for o in (offices or [])
        if (o.get("location") or "").strip()
    ]
    return "; ".join(dict.fromkeys(locs)) or "India"


def _fetch_page(page: int, timeout: int) -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(
                _API_URL,
                headers=_HEADERS,
                params={
                    "accountName": _TENANT,
                    "page": page,
                    "type": "ats_job_post.index",
                    "items": _PAGE_SIZE,
                },
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("slice: 429 rate-limited from Kula API")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"slice Kula API call failed: {exc}") from exc
    raise RateLimitError(f"slice: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate the whole Kula board once and cache every job.

    ``_cache_filled`` is set True before the loop so a transient failure
    does not trigger a retry-storm on every subsequent fetch_jobs() call in
    the same process (Honeywell/CRED/Razorpay lesson — PLAYBOOK §Key Bugs).
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    total_pages = 1
    page = 1
    while page <= total_pages and page <= _MAX_PAGES:
        data = _fetch_page(page, timeout=timeout)
        meta = data.get("meta") or {}
        total_pages = int(meta.get("pages") or 1)
        raw_jobs = data.get("data") or []
        if not raw_jobs:
            break

        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or "").strip()
            if not (job_id and title) or job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            ats_job = j.get("ats_job") or {}
            location = _location_from_offices(ats_job.get("offices") or [])
            posting_date = (j.get("launch_at") or "")[:10]

            _description_cache[job_id] = ats_job.get("job_description") or ""

            collected.append({
                "id": job_id,
                "title": title,
                "location": location,
                "posting_date": posting_date,
                "application_url": f"{_JOB_PAGE_BASE}{job_id}",
            })

        page += 1
        if page <= total_pages:
            time.sleep(0.2)

    _job_cache = collected
    print(f"[slice] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of slice's (Kula) job postings.

    ``keyword``/``location`` are accepted for interface compatibility but
    not sent to the API — the whole board is cached once per process and
    matcher.py's title/skill/India filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single slice job.

    Served entirely from the cache filled by ``_fill_cache`` — the Kula
    list API already includes each job's full HTML description inline, so
    no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").split("/")[-1]
    description = _strip_html(_description_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
