"""Fetches Unacademy job listings — Darwinbox candidatev2 tenant "unacademy".

ATS discovery (2026-09-14): unacademy.com/careers is a Next.js marketing
page whose careers CTA links to
``https://unacademy.darwinbox.in/ms/candidate/careers``, which in turn
redirects to the newer "candidatev2" SPA
(``https://unacademy.darwinbox.in/ms/candidatev2/main/careers/allJobs``) —
same product tier as this repo's own ``darwinbox_fetcher.py`` (Darwinbox
hiring for itself, tenant "dbx") and Perfios/Signzy, NOT the older
"candidate" SPA used by Zomato/Sonata Software/upGrad.

Cloudflare gating — confirmed live (2026-09-14): a bare ``requests.post()``
with a realistic browser User-Agent (and Referer/Accept headers) still gets
a flat HTTP 403 Cloudflare "Attention Required!" block page on both
``/ms/candidateapi/companyinfo`` and the job-list API — a stronger gate
than the "dbx" tenant's UA-string-only block (that one passes with a plain
``requests`` call once a real UA is set; this one does not). Per this
repo's established convention for any Darwinbox tenant showing Cloudflare
signal, this fetcher drives real headless Firefox via Playwright, reusing
one Cloudflare-cleared page's ``fetch()`` (same technique as
``darwinbox_fetcher.py`` — a blank page has no origin of its own, so a
POST with a JSON Content-Type triggers a CORS preflight that fails; a page
already navigated to the tenant makes it a same-origin call instead).

Real API endpoint (identical shape to darwinbox_fetcher.py, confirmed via
live Playwright network capture of ``/ms/candidatev2/main/careers/allJobs``):

    POST /ms/candidateapi/job/alljobs?companyId=main
    Body: {"companyId": "main", "page": N, "sort_option": "new", "limit": M}
        -> {"status": "success", "job_counts": <int total>,
            "data": [{id, title, jd (HTML, INLINE — no separate detail call
                      needed), officelocation_show_arr, tool_tip_locations,
                      country, posted_on, ...}, ...]}

No keyword or location param exists in the real SPA's own requests (same
"ignored server-side" pattern as every other Darwinbox tenant in this
repo) — the whole board is paginated through once per process and cached;
matcher.py does the real title/skill/India filtering.

**Current live state (2026-09-14): genuinely 7 total open postings,
ZERO of which are engineering/tech roles.** All 7 are Business/Growth/
Sales roles (Lead Community Management, Business Development Executive,
Assistant VP Growth, Category Associate, Associate Director Business/
Growth/Sales) under the "Graphy - Business"/"Business"/"IST Team"
departments — Unacademy's edtech-adjacent SaaS spinoff Graphy plus core
GTM functions. `job/filters` confirms only 3 India locations exist across
the whole board (Bangalore, Noida, Remote) and zero tech/engineering
department facet. This is a genuine "zero is a fact" result, not a
fetcher bug — same precedent as Darwinbox's own tenant/Zomato-eternal/
Perfios/ING/eClerx elsewhere in this repo. The pipeline is mechanically
correct (verified live end-to-end: cache fill, pagination-end detection,
description inline) and will surface a real engineering opening the
moment Unacademy posts one through this same tenant.

Location: ``tool_tip_locations`` is the clean form (no stray ``\\r`` or
trailing office-code parenthetical); falls back to
``officelocation_show_arr`` with the code suffix stripped, same as
darwinbox_fetcher.py.

Description: ``jd`` is inline HTML in the list response — some postings
carry a literal placeholder string ("Please enter job description")
instead of real content; harmless since these postings don't match
title_family anyway, and any future engineering posting can be
re-verified once live.

Application URL: ``https://unacademy.darwinbox.in/ms/candidatev2/main/
careers/jobDetails/{id}?from=all`` — same route-name pattern verified
live for the "dbx" tenant; not independently click-verified here since
Unacademy's SPA is otherwise byte-identical Darwinbox candidatev2 product
code (only the JSON data differs), same reasoning already documented in
darwinbox_fetcher.py.
"""
from __future__ import annotations

import atexit
import html as html_mod
import json
import re
import time
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_TENANT = "unacademy"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidatev2/main/careers/allJobs"
_ALLJOBS_API = f"{_BASE}/ms/candidateapi/job/alljobs?companyId=main"
_JOB_PAGE_BASE = f"{_BASE}/ms/candidatev2/main/careers/jobDetails/"

_PAGE_SIZE = 50
_MAX_PAGES = 20  # safety cap (~1 000 jobs) well beyond this board's size

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

_FETCH_JS = """async (args) => {
    const [url, body] = args;
    const resp = await fetch(url, {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox required, Cloudflare blocks plain requests
# ---------------------------------------------------------------------------

_pw = None
_browser = None
_live_context = None   # Cloudflare-cleared context; kept alive after fill
_live_page = None      # page navigated to _CAREERS_PAGE — see note below


def _ensure_browser() -> None:
    global _pw, _browser
    if not _PLAYWRIGHT_AVAILABLE:
        raise RateLimitError(
            "playwright not installed — run: "
            "pip install playwright && playwright install firefox"
        )
    if _browser is None:
        with STARTUP_LOCK:
            _pw = sync_playwright().start()
            try:
                _browser = _pw.firefox.launch(headless=True)
            except Exception:
                _pw.stop()
                _pw = None
                raise
        atexit.register(_shutdown_browser)


def _shutdown_browser() -> None:
    global _pw, _browser, _live_context, _live_page
    try:
        if _live_context:
            _live_context.close()
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _live_page = None
    _live_context = None
    _browser = None
    _pw = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


_CODE_SUFFIX_RE = re.compile(r"\s*\([A-Z0-9]{2,6}_[A-Z0-9]+\)\s*$")


def _location_from_job(job: dict) -> str:
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    raw = (job.get("officelocation_show_arr") or "").replace("\r", "").strip()
    raw = _CODE_SUFFIX_RE.sub("", raw).strip()
    return raw or (job.get("country") or "").strip() or "India"


def _ensure_live_context(timeout: int = 30) -> None:
    """Create (or re-create) the module-level Cloudflare-cleared context.

    Keeps the navigated page open (rather than closing it) so subsequent
    same-origin ``fetch()`` calls avoid a CORS preflight failure — see
    darwinbox_fetcher.py's identical note for why a blank page doesn't work.
    """
    global _live_context, _live_page
    _ensure_browser()
    if _live_context is not None and _live_page is not None:
        return  # already cleared, reuse

    ctx = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = ctx.new_page()
    try:
        page.goto(_CAREERS_PAGE, wait_until="networkidle", timeout=timeout * 1000)
    except PWTimeoutError:
        page.goto(_CAREERS_PAGE, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(4000)

    _live_context = ctx
    _live_page = page  # kept open deliberately — see docstring above


def _call_api(url: str, body: dict, timeout: int = 20) -> dict:
    global _live_context, _live_page
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            result = _live_page.evaluate(_FETCH_JS, [url, body])

            if result["status"] == 429:
                raise RateLimitError(f"Unacademy: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(
                    f"Unacademy API {url!r} returned HTTP {result['status']}"
                )
            return json.loads(result["text"])

        except RateLimitError:
            raise
        except Exception as exc:
            last_exc = exc
            try:
                if _live_context:
                    _live_context.close()
            except Exception:
                pass
            _live_context = None
            _live_page = None
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Unacademy API call failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"Unacademy: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache — paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


def _fill_cache(timeout: int = 30) -> None:
    """Paginate through the entire Darwinbox board once and cache every job.

    ``_cache_filled`` is set True before the loop so a transient failure
    does not trigger a retry-storm on every subsequent fetch_jobs() call in
    the same process (Honeywell/CRED/Razorpay lesson — PLAYBOOK §Key Bugs).
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    _ensure_live_context(timeout=timeout)

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None
    total_expected: int | None = None

    for page_num in range(1, _MAX_PAGES + 1):
        body = {
            "companyId": "main",
            "page": page_num,
            "sort_option": "new",
            "limit": _PAGE_SIZE,
        }
        data = _call_api(_ALLJOBS_API, body, timeout=timeout)
        raw_jobs: list = (data or {}).get("data") or []
        if total_expected is None:
            total_expected = (data or {}).get("job_counts")
        if not raw_jobs:
            break

        page_ids = {str(j.get("id") or "") for j in raw_jobs if j.get("id")}

        if page_num == 1:
            first_page_ids = page_ids
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
    print(f"[Unacademy] Cache filled: {len(collected)} total jobs")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py / run_company.py
# ---------------------------------------------------------------------------


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a slice of Unacademy's Darwinbox postings.

    ``keyword``/``location`` are accepted for interface compatibility but
    are not sent to the API — this tenant's ``alljobs`` endpoint ignores
    keyword params (verified against the identical "dbx" tenant elsewhere
    in this repo) and no location param exists in the real SPA's own
    requests. The full board is paginated through once per process and
    cached; matcher.py does the real filtering.
    """
    if not _cache_filled:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                _fill_cache(timeout=timeout)
                break
            except RateLimitError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            if last_exc:
                raise RateLimitError(
                    f"Unacademy cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Unacademy job.

    Served entirely from the cache filled by ``fetch_jobs`` — the
    ``alljobs`` API already returns the full ``jd`` HTML for every posting
    in one call, so no separate per-job detail request is needed.
    """
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
