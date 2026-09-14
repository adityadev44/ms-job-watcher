"""Fetches upGrad job listings — Darwinbox candidate (legacy) tenant "upgrad".

ATS discovery (2026-09-14): upgrad.com/careers/ is a marketing page whose
careers CTAs link to ``https://upgrad.darwinbox.in/ms/candidate/careers``
and ``.../ms/candidate/main/careers`` — the OLDER "candidate" SPA (same
product tier as ``zomato_fetcher.py``'s "eternal" tenant and Ola Cabs, per
that module's docstring), NOT the newer "candidatev2" SPA used by this
repo's own Darwinbox/Unacademy fetchers.

Cloudflare gating — confirmed live (2026-09-14): unlike Zomato's "eternal"
tenant (which answers a plain ``requests.get()`` with a realistic UA with
no Cloudflare challenge at all), upGrad's tenant returns a flat HTTP 403
Cloudflare "Attention Required!" page to the identical plain-`requests`
call. Per this repo's Darwinbox convention, this fetcher drives headless
Firefox via Playwright and issues the API calls as same-origin
``fetch()`` calls from a page already navigated to the tenant (same
CORS-preflight reasoning documented in ``darwinbox_fetcher.py``).

Real API endpoints (identical shape to zomato_fetcher.py's "candidate"
SPA, confirmed via live Playwright network capture + direct in-page
``fetch()`` probes of ``/ms/candidate/careers``):

    GET /ms/candidateapi/job?page=N&limit=M&companyId=main
        -> {"status": "success",
            "message": {"jobscount": <int>, "jobs": [...]}}
    GET /ms/candidateapi/job/{id}?companyId=main
        -> {"status": "success", "message": {"job": [{...one job...}]}}
        (description NOT inline in the list response — separate detail
        call required, same as Zomato)
    GET /ms/candidateapi/job/filters?companyId=main
        -> facet values

Unlike Unacademy's/Darwinbox's own candidatev2 tenants, this legacy SPA's
job-list endpoint requires an explicit ``companyId=main`` query param —
omitting it returns HTTP 404 `{"message":"Expecting a valid Company
value"}` (confirmed live; the candidatev2 tenants default this from the
URL path instead).

**Current live state (2026-09-14): genuinely ZERO open postings across
the entire tenant, not a fetcher bug.** ``companyinfo`` confirms the
tenant is live (`"company_name":"upGrad Education Pvt. Ltd."`,
`"recruitment_enabled":true`) but ``job/filters`` returns every facet
empty (`"locations":[{"_id":"is_remote","name":"Remote"}]` only,
`"departments":[]`) and ``job?page=1&limit=20&companyId=main`` returns
`{"jobscount":0,"jobs":[]}`. This is the identical "recruitment_enabled
but zero postings on this legacy SPA tier" pattern already documented in
``zomato_fetcher.py`` (Eternal) and its Ola Cabs cross-check — every
``new_careers:false``-tier Darwinbox tenant found in this repo's history
sits at zero. Field shapes below are carried over unmodified from
zomato_fetcher.py (verified against Zepto/Hetero, populated siblings of
the exact same legacy SPA product) since upGrad's own board has no live
postings to click-verify against right now — same "one real gap, called
out rather than papered over" acknowledgment as Zomato's docstring.
Re-verify field shapes and the application URL construction the first
time upGrad posts a real job through this pipeline.

Application URL: best-effort construction
``https://upgrad.darwinbox.in/ms/candidate/careers/job/{id}`` mirroring
the API's own literal route-name map, same unverified-but-consistent
reasoning as Zomato/Eternal.

Description: ``jd`` is HTML-entity-escaped one extra level, same idiom as
Zomato/Razorpay/Groww — unescape, strip tags, unescape again.
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

_TENANT = "upgrad"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidate/careers"
_JOB_LIST_URL = f"{_BASE}/ms/candidateapi/job?companyId=main"
_JOB_DETAIL_BASE = f"{_BASE}/ms/candidateapi/job/"
_JOB_PAGE_BASE = f"{_CAREERS_PAGE}/job/"

_PAGE_SIZE = 50
_MAX_PAGES = 20  # safety cap well beyond any plausible pool size

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

_GET_JS = """async (url) => {
    const resp = await fetch(url, {method: 'GET', credentials: 'include'});
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
_live_context = None
_live_page = None


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


def _location_from_job(job: dict) -> str:
    loc = (job.get("officelocation_show_arr") or "").strip()
    if loc and loc.lower() != "multiple locations":
        return loc
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    return loc or "India"


def _ensure_live_context(timeout: int = 30) -> None:
    global _live_context, _live_page
    _ensure_browser()
    if _live_context is not None and _live_page is not None:
        return

    ctx = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = ctx.new_page()
    try:
        page.goto(_CAREERS_PAGE, wait_until="networkidle", timeout=timeout * 1000)
    except PWTimeoutError:
        page.goto(_CAREERS_PAGE, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(4000)

    _live_context = ctx
    _live_page = page


def _call_api(url: str, timeout: int = 20, context: str = "") -> dict:
    global _live_context, _live_page
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            result = _live_page.evaluate(_GET_JS, url)

            if result["status"] == 429:
                raise RateLimitError(f"upGrad {context}: 429 rate-limited")
            if result["status"] != 200:
                raise RateLimitError(
                    f"upGrad {context} returned HTTP {result['status']}"
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
                f"upGrad {context} failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"upGrad {context}: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache — paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_detail_cache: dict[str, dict] = {}
_cache_filled: bool = False


def _fill_cache(timeout: int = 30) -> None:
    """Paginate through the entire Darwinbox board once and cache it.

    ``_cache_filled`` is set True before the loop so a transient failure
    does not retry-storm on every subsequent fetch_jobs() call in the same
    process (Honeywell/CRED/Razorpay lesson — PLAYBOOK §Key Bugs).
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    _ensure_live_context(timeout=timeout)

    collected: list[dict] = []
    for page_num in range(1, _MAX_PAGES + 1):
        data = _call_api(
            f"{_JOB_LIST_URL}&page={page_num}&limit={_PAGE_SIZE}",
            timeout=timeout,
            context=f"job list page {page_num}",
        )
        raw_jobs = ((data or {}).get("message") or {}).get("jobs") or []
        if not raw_jobs:
            break
        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or "").strip()
            if not (job_id and title):
                continue
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": _epoch_to_date(j.get("job_posting_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}",
            })
        if len(raw_jobs) < _PAGE_SIZE:
            break
        if page_num < _MAX_PAGES:
            time.sleep(0.1)

    _job_cache = collected
    print(f"[upGrad] Cache filled: {len(collected)} total jobs")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py / run_company.py
# ---------------------------------------------------------------------------


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of upGrad (Darwinbox "upgrad" tenant) postings.

    keyword/location are accepted for interface compatibility but ignored
    server-side (this legacy SPA's job-list endpoint has no keyword param
    in the real page's own requests, same as Zomato/Eternal); the shared
    matcher does the real title/skill/India filtering. The whole board is
    paginated through and cached once per process.
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
                    f"upGrad cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    return application_url.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single upGrad job.

    Descriptions are NOT included in the list response — a separate
    detail call is required and cached by job id.
    """
    job_id = _job_id_from_url(application_url)

    detail = _detail_cache.get(job_id)
    if detail is None:
        data = _call_api(
            f"{_JOB_DETAIL_BASE}{job_id}?companyId=main",
            timeout=timeout,
            context=f"job detail {job_id}",
        )
        job_list = ((data or {}).get("message") or {}).get("job") or []
        detail = job_list[0] if job_list else {}
        _detail_cache[job_id] = detail

    description = _strip_html(detail.get("jd") or "")
    posting_date = _epoch_to_date(detail.get("posted_on") or detail.get("job_posting_on"))
    return description, posting_date
