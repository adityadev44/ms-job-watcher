"""HCA Healthcare India official hcahr Darwinbox candidatev2 board.
Official hcahealthcare.in/careers links this exact tenant. Verified live
2026-10-09: POST alljobs with companyId=main, page, sort_option=new,
limit=50 returns119 jobs and inline JDs. Plain requests blocked403;
Firefox page-origin replay confirmed200. No guessing of tenant or API.
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

_TENANT = "hcahr"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidatev2/main/careers/allJobs"
_ALLJOBS_API = f"{_BASE}/ms/candidateapi/job/alljobs?companyId=main"
_JOB_PAGE_BASE = f"{_BASE}/ms/candidatev2/main/careers/jobDetails/"
_PAGE_SIZE = 50
_MAX_PAGES = 20
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
    """Prefer the clean tooltip list — ``officelocation_show_arr`` on this
    tenant embeds a stray literal "\\r" and a trailing city-code
    parenthetical (e.g. "Hyderabad, Telangana\\r, India (IND_HYD)") that
    ``tool_tip_locations`` doesn't carry."""
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    raw = (job.get("officelocation_show_arr") or "").replace("\r", "").strip()
    raw = _CODE_SUFFIX_RE.sub("", raw).strip()
    return raw or (job.get("country") or "").strip() or ""


def _ensure_live_context(timeout: int = 30) -> None:
    """Create (or re-create) the module-level Cloudflare-cleared context.

    Navigates to the careers page once to solve the Cloudflare challenge and
    store its cookies in the context, then KEEPS THAT SAME PAGE OPEN (rather
    than closing it) as ``_live_page`` for subsequent API calls.

    This differs from sonatasoftware_fetcher.py's ``_call_api``, which opens
    a fresh blank ``new_page()`` per call — that works fine for a plain GET
    with no custom headers (a CORS "simple request"), but this tenant's
    ``alljobs`` endpoint is a POST with a ``Content-Type: application/json``
    body, which triggers a CORS preflight. A blank ``about:blank`` page has
    no origin of its own, and that preflight fails with a bare "NetworkError"
    (confirmed live) — while the SAME request issued from a page that has
    actually navigated to ``hcahr.darwinbox.in`` succeeds immediately, because
    it's then a same-origin call with no CORS preflight involved at all.
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
    """POST a JSON body to an API URL via the Cloudflare-cleared browser
    page and return the parsed JSON dict.

    Raises ``RateLimitError`` on HTTP 429, any other non-200 status, or a
    JSON-parse failure after retries are exhausted.
    """
    global _live_context, _live_page
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            result = _live_page.evaluate(_FETCH_JS, [url, body])

            if result["status"] == 429:
                raise RateLimitError(f"HCA Healthcare: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(
                    f"HCA Healthcare API {url!r} returned HTTP {result['status']}"
                )
            return json.loads(result["text"])

        except RateLimitError:
            raise
        except Exception as exc:
            last_exc = exc
            # Context/page may have gone stale — discard both so
            # _ensure_live_context will re-create them on the next attempt.
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
                f"HCA Healthcare API call failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"HCA Healthcare: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache — paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


def _fill_cache_unchecked(timeout: int = 30) -> None:
    """Paginate through the entire HCA Healthcare board once and cache every job.

    ``_cache_filled`` is set True before the loop so a transient failure
    does not trigger a retry-storm on every subsequent fetch_jobs() call in
    the same process (Honeywell/CRED/Razorpay lesson — PLAYBOOK §Key Bugs).
    """
    global _cache_filled, _job_cache, _FIRST_PAGE_IDS
    if _cache_filled:
        return
    _cache_filled = True

    # Ensure the Cloudflare context is live before entering the loop so the
    # first API call doesn't pay the navigation overhead inside _call_api.
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
            break  # empty page → past the end of the board

        page_ids = {str(j.get("id") or "") for j in raw_jobs if j.get("id")}

        # Wraparound guard
        if page_num == 1:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1 — stop here

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
            # All IDs on this page already seen — wraparound-style dedupe guard.
            break

        if isinstance(total_expected, int) and len(collected) >= total_expected:
            break  # collected everything the API says exists

        if page_num < _MAX_PAGES:
            time.sleep(0.1)  # be polite between pages

    if isinstance(total_expected, int) and len(collected) < total_expected:
        raise RateLimitError(f"HCA Healthcare incomplete board: {len(collected)} of {total_expected} jobs")
    _job_cache = collected
    print(f"[HCA Healthcare] Cache filled: {len(collected)} total jobs")


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
    """Return a slice of HCA Healthcare's own postings.

    ``keyword`` and ``location`` are accepted for interface compatibility
    but are deliberately not sent to the API. The verified initial request
    uses companyId/page/sort_option/limit, and the complete India board is small.
    The full board is
    paginated through once per process and cached; all narrowing is done by
    matcher.py's title/skill/India filters.
    """
    if _cache_error is not None:
        raise _cache_error
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
                    f"HCA Healthcare cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single HCA Healthcare job.

    Served entirely from the cache filled by ``fetch_jobs`` — the ``alljobs``
    API already returns the full ``jd`` HTML for every posting in one call,
    so no separate per-job detail request is needed (same as Perfios).
    """
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    _fill_cache(timeout)
    if job_id not in _desc_cache:
        raise RateLimitError(f"HCA Healthcare posting no longer listed: {job_id}")
    description = _desc_cache[job_id]
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date

_cache_error = None

def _fill_cache(timeout=30):
    global _cache_error
    if _cache_error is not None:
        raise _cache_error
    try:
        _fill_cache_unchecked(timeout)
    except Exception as exc:
        _cache_error = RateLimitError(f"HCA Healthcare cache fill failed: {exc}")
        raise _cache_error from exc
