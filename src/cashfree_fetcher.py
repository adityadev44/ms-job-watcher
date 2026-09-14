"""Cashfree Payments job fetcher — Kula ATS.

ATS discovery (2026-09-14): www.cashfree.com/careers embeds an iframe:

    <iframe id="kula_embed" title="Cashfree Careers Job Listings"
            src="https://careers.kula.ai/cashfree?jobs=true">

Same "Kula" ATS vendor and same first-party JSON API discovered for
``slice_fetcher.py`` (see that module's docstring for the full API-shape
writeup and how it was found via Playwright network capture rather than
static bundle inspection):

    GET https://careers.kula.ai/api/internal/ats_job_posts
            ?accountName=cashfree&page=N&type=ats_job_post.index&items=100
        -> {"data": [...], "meta": {"count", "page", "items", "pages"}}

Confirmed live with plain `requests.get()` — no auth/cookies/Cloudflare
gating. 31 total postings live at investigation time, real engineering
titles present (Software Development Engineer 2/3, Software Development
Lead, Data Scientist-3, Principal Security Engineer, Frontend Engineer 2,
SDET-1), all in Bengaluru (Bellandur) or Gurgaon — no excluded cities
observed.

Each item's shape is identical to slice's tenant: ``ats_job.job_description``
is full inline HTML (no separate detail call needed), and
``ats_job.offices[].location`` is already a complete "City, State, India"
string.

No server-side keyword filtering exercised; whole (small) board cached
once per process, matcher.py's title/skill layers do the real narrowing.

Application URL: ``https://careers.kula.ai/cashfree/{id}``.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_TENANT = "cashfree"
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
                raise RateLimitError("Cashfree: 429 rate-limited from Kula API")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Cashfree Kula API call failed: {exc}") from exc
    raise RateLimitError(f"Cashfree: no response — {last_exc}")


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
    print(f"[Cashfree] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of Cashfree's (Kula) job postings.

    ``keyword``/``location`` are accepted for interface compatibility but
    not sent to the API — the whole board is cached once per process and
    matcher.py's title/skill/India filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Cashfree job.

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
