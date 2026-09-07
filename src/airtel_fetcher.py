"""Fetches Bharti Airtel job listings — Darwinbox candidate portal, tenant
"airtel", running the same "candidatev2" SPA product tier as Darwinbox's
own board (``darwinbox_fetcher.py``, tenant "dbx") and Perfios/Signzy in
this repo -- NOT the older "candidate" SPA used by Zomato/Sonata/Vedanta.

ATS discovery (live, 2026-09-07): careers.airtel.com is a React SPA whose
own client-side config JSON (``main.<hash>.js``) embeds
``"darwinboxURL":"https://airtel.darwinbox.in/ms/candidatev2/main/careers/
allJobs"``, and the page's own "Jobs" nav link points there directly --
confirmed live via Playwright.

Unlike Darwinbox's OWN "dbx" tenant (Cloudflare-gated, requires Playwright/
Firefox — see ``darwinbox_fetcher.py``), this tenant answers plain
``requests.post()`` directly with no bot-management challenge at all —
confirmed live (2026-09-07) across multiple fresh, cookie-less calls. No
Playwright needed here; same "open" tier as ``zomato_fetcher.py``'s
Darwinbox tenant.

API (same shape as ``darwinbox_fetcher.py``'s own tenant — this is a
shared Darwinbox "candidatev2" product, not tenant-specific):

    POST /ms/candidateapi/job/alljobs?companyId=main
    Body: {"companyId": "main", "page": N, "sort_option": "new", "limit": M}
        -> {"status": "success", "job_counts": <int total>,
            "data": [{id, title, jd (HTML, INLINE — no separate detail
                      call needed), officelocation_show_arr,
                      tool_tip_locations, country, posted_on, ...}, ...]}

Server-side filtering — tested live against the real API: no keyword param
exists. ``search``/``keyword``/``q``/``query``/``searchString`` were all
tried in the POST body; every one is silently ignored — ``job_counts``
stays 57 regardless. Keywords are IGNORED server-side (same finding as
Darwinbox's own "dbx" tenant); matcher.py's title/skill filters do the real
narrowing. No location param was attempted for the same "don't guess a
param that might silently zero real results" reason as Darwinbox's own
tenant.

Current live state (2026-09-07): 57 total open postings, ALL India (no
overseas postings observed on this tenant at all — ``country`` is "India"
on every single one). The board skews heavily Sales/GTM/Network-Ops/
Account-Management (Territory Manager, Account Manager - B2B Sales, Circle
Partnership, Network Account Manager, etc.) with only a handful of
digital/tech-adjacent titles today (Solution Architect x2, IOT Specialist,
Product Manager x4) and zero postings that literally match this repo's
default `.NET`/AI-ML/Python title_family keywords right now. This is a
genuine "zero is a fact" result for this specific Darwinbox tenant (same
precedent as Darwinbox's own board and Vedanta's tenant elsewhere in this
repo) — Airtel's own tech-heavy digital arms (Airtel Digital, Nxtra Data,
Airtel Payments Bank) do not appear to route through this particular
candidate tenant today. The pipeline is mechanically correct and will
surface a real match automatically the moment a matching title is posted
here.

Location: ``officelocation_show_arr``/``tool_tip_locations`` already carry
clean "City, State, India" strings (a few multi-branch postings list many
cities, joined with "; ", same handling as Darwinbox's own tenant) — no
hand-maintained city list needed since every job here also carries a
reliable ``country: "India"`` field, used as the final fallback.

Application URL: ``https://airtel.darwinbox.in/ms/candidatev2/main/careers/
jobDetails/{id}?from=all`` (same convention as Darwinbox's own tenant).

Description: ``jd`` is HTML-entity-escaped one extra level (raw text
starts ``&lt;p&gt;``), same idiom as Darwinbox/Zomato/Razorpay/Groww/
Perfios/Sonata in this repo — unescape, strip tags, unescape again.
Already inline in the list response, so ``fetch_job_description`` is
served entirely from the cache filled by ``fetch_jobs`` — no extra
per-job API call needed for jobs already seen this process.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timezone

import requests

_TENANT = "airtel"
_BASE = f"https://{_TENANT}.darwinbox.in"
_ALLJOBS_API = f"{_BASE}/ms/candidateapi/job/alljobs?companyId=main"
_JOB_PAGE_BASE = f"{_BASE}/ms/candidatev2/main/careers/jobDetails/"

_PAGE_SIZE = 50
_MAX_PAGES = 20  # safety cap (~1000 jobs) well beyond this ~57-job board

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}

_CODE_SUFFIX_RE = re.compile(r"\s*\([A-Za-z0-9]{2,10}_[A-Za-z0-9]+\)\s*$")

# Module-level cache: keyword is ignored server-side (verified live), so
# the whole (tiny) board is paginated through once per process.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from Airtel's Darwinbox tenant."""


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
    """Prefer the clean tooltip list; officelocation_show_arr on this
    tenant sometimes trails a branch-code parenthetical (e.g. "...
    (APBL_300000367251403)") that tool_tip_locations doesn't carry."""
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    raw = (job.get("officelocation_show_arr") or "").replace("\r", "").strip()
    raw = _CODE_SUFFIX_RE.sub("", raw).strip()
    return raw or (job.get("country") or "").strip() or "India"


def _call_api(body: dict, timeout: int) -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(_ALLJOBS_API, json=body, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Airtel: 429 rate-limited during cache fill")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Airtel cache fill failed: {exc}") from exc
    raise RateLimitError(f"Airtel cache fill: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through the entire Darwinbox board once and cache it.

    ``_cache_filled`` is set before the loop so a mid-fetch failure doesn't
    trigger a retry storm on every subsequent keyword call.
    """
    global _cache_filled, _job_cache, _FIRST_PAGE_IDS, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None
    total_expected: int | None = None

    for page_num in range(1, _MAX_PAGES + 1):
        body = {"companyId": "main", "page": page_num, "sort_option": "new", "limit": _PAGE_SIZE}
        try:
            data = _call_api(body, timeout=timeout)
        except RateLimitError as exc:
            _cache_error = exc
            raise

        raw_jobs: list = (data or {}).get("data") or []
        if total_expected is None:
            total_expected = (data or {}).get("job_counts")
        if not raw_jobs:
            break

        page_ids = {str(j.get("id") or "") for j in raw_jobs if j.get("id")}

        if page_num == 1:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1

        new_this_page = 0
        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or "").strip()
            if not (job_id and title) or job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            new_this_page += 1
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": _epoch_to_date(j.get("posted_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}?from=all",
            })
            _desc_cache[job_id] = _strip_html(j.get("jd") or "")

        if new_this_page == 0:
            break

        if isinstance(total_expected, int) and len(collected) >= total_expected:
            break

        if page_num < _MAX_PAGES:
            time.sleep(0.1)

    _job_cache = collected
    print(f"[Airtel] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Bharti Airtel jobs.

    Keywords are ignored (see module docstring) -- the whole board (~57
    postings) is cached once per process and matcher.py's shared
    title/skill filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Airtel job.

    Already inline in the list response and cached by job id during
    fetch_jobs -- no extra network call needed for a job already seen
    this process (same "cache filled by fetch_jobs" idiom as
    darwinbox_fetcher.py/Perfios). Falls back to empty strings (never
    raises) for a job id this process hasn't cached.
    """
    _fill_cache(timeout=timeout)
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
