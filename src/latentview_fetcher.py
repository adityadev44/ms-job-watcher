"""Fetches LatentView Analytics job listings via its Darwinbox tenant
"latentview" — the same product family (and, for the job-list API, the
exact same request shape) as `darwinbox_fetcher.py` (Darwinbox's own
tenant "dbx"), running the newer "candidatev2" SPA.

ATS discovery: `www.latentview.com/career/` links to
`https://latentview.darwinbox.in/ms/candidate/careers`, which itself
redirects to the "candidatev2" SPA once loaded in a real browser
(`/ms/candidatev2/careers`). Confirmed via Playwright network capture,
clicking "Open Jobs" then "View and Apply" on a real posting.

Cloudflare gating -- this tenant is materially harder than most: a plain
`requests.get()`/`.post()` (any User-Agent, including a realistic desktop
Chrome string) gets a flat HTTP 403 Cloudflare block page even for the
static SPA shell HTML, and the job-list API 403s the same way. This is
NOT just a UA-string check (unlike dbx's own tenant) -- it held even with a
full browser-matching UA header on a bare `requests` session, so it's a
TLS-fingerprint/JS-challenge-tier block. A real headless browser (both
Chromium with a non-"HeadlessChrome" UA override and headless Firefox were
confirmed to pass) clears it fine. Follows this repo's established
Cloudflare-gated-Darwinbox convention (same as `darwinbox_fetcher.py`,
`sonatasoftware_fetcher.py`, `perfios_fetcher.py`): drive real headless
Firefox via Playwright for every call rather than relying on a
plain-`requests` path that happens to work today from one IP.

Real API endpoint (identical shape to dbx's own tenant, found via the same
live Playwright network capture technique -- load `/ms/candidatev2/careers`,
click "Open Jobs", observe the XHR):

    POST /ms/candidateapi/job/alljobs?companyId=main
    Body: {"companyId": "main", "page": N, "sort_option": "new", "limit": M}
        -> {"status": "success", "job_counts": <int total>,
            "data": [{id, title, designation_display_name, jd (HTML,
                      INLINE -- no separate detail call needed), country,
                      tool_tip_locations, posted_on, ...}, ...]}

Server-side filtering: no keyword param exists in the SPA's own requests
(same as dbx) -- the full ~37-job global board (India + US openings mixed)
is paginated through once per process and cached; matcher.py's title/
skill/India filters do the real narrowing. Unlike dbx's tenant, this one
DOES expose a reliable `country` field directly on every job ("India",
"United States", ...) -- used for India filtering client-side, no city-
name guessing needed. `tool_tip_locations` gives a clean
"City, State, India" string for the location field; India cities seen live
include Bengaluru, Chennai, and "Remote" postings tagged Haryana/
Telangana/Karnataka -- Chennai is excluded downstream via
`exclude_locations` like every other company in this repo.

Description: `jd` is HTML-entity-escaped one extra level (raw text starts
`&lt;p ...&gt;`), same idiom as Zomato/Razorpay/Groww/dbx -- unescape,
strip tags, unescape again. Already inline in the list response, so
`fetch_job_description` is served entirely from the cache filled by
`fetch_jobs` -- no extra per-job API call.

Application URL: `https://latentview.darwinbox.in/ms/candidatev2/main/
careers/jobDetails/{id}?from=all` -- confirmed live via Playwright
click-through on a real "View and Apply" link (identical path shape to
dbx's own tenant, just a different subdomain).
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

_TENANT = "latentview"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidatev2/careers"
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
# Browser singleton -- Firefox required, Cloudflare blocks plain requests
# ---------------------------------------------------------------------------

_pw = None
_browser = None
_live_context = None   # Cloudflare-cleared context; kept alive after fill
_live_page = None      # page navigated to _CAREERS_PAGE -- see note below


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
    """Prefer the clean tooltip list; fall back to the plain `locations`
    string, then the bare `country` field."""
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    loc = (job.get("locations") or "").replace("\r", "").strip()
    return loc or (job.get("country") or "").strip() or "India"


def _is_india_job(job: dict) -> bool:
    country = (job.get("country") or "").strip().lower()
    if country:
        return country == "india"
    # Fall back to a plain substring check if country is ever missing.
    return "india" in (job.get("locations") or "").lower()


def _ensure_live_context(timeout: int = 30) -> None:
    """Create (or re-create) the module-level Cloudflare-cleared context.

    Navigates to the careers page once to solve the Cloudflare challenge and
    store its cookies in the context, then KEEPS THAT SAME PAGE OPEN (rather
    than closing it) as `_live_page` for subsequent API calls -- the
    `alljobs` endpoint is a POST with a `Content-Type: application/json`
    body, which triggers a CORS preflight that fails from a blank
    `about:blank` page but succeeds from a page that has actually navigated
    to `latentview.darwinbox.in` (same-origin call), same lesson as
    `darwinbox_fetcher.py`.
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
    _live_page = page  # kept open deliberately -- see docstring above


def _call_api(url: str, body: dict, timeout: int = 20) -> dict:
    """POST a JSON body to an API URL via the Cloudflare-cleared browser
    page and return the parsed JSON dict.

    Raises `RateLimitError` on HTTP 429, any other non-200 status, or a
    JSON-parse failure after retries are exhausted.
    """
    global _live_context, _live_page
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            result = _live_page.evaluate(_FETCH_JS, [url, body])

            if result["status"] == 429:
                raise RateLimitError(f"LatentView: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(
                    f"LatentView API {url!r} returned HTTP {result['status']}"
                )
            return json.loads(result["text"])

        except RateLimitError:
            raise
        except Exception as exc:
            last_exc = exc
            # Context/page may have gone stale -- discard both so
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
                f"LatentView API call failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"LatentView: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache -- paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


def _fill_cache(timeout: int = 30) -> None:
    """Paginate through the entire LatentView board once, filter to India,
    and cache every job.

    `_cache_filled` is set True before the loop so a transient failure does
    not trigger a retry-storm on every subsequent fetch_jobs() call in the
    same process (Honeywell/CRED/Razorpay lesson -- PLAYBOOK "Key Bugs").
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
            break  # empty page -> past the end of the board

        page_ids = {str(j.get("id") or "") for j in raw_jobs if j.get("id")}

        # Pagination-wraparound guard (repo-standard contract).
        if page_num == 1:
            first_page_ids = page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1 -- stop here

        new_this_page = 0
        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or j.get("designation_display_name") or "").strip()
            if not (job_id and title) or job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            new_this_page += 1
            if not _is_india_job(j):
                continue  # global board -- keep only India postings
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": _epoch_to_date(j.get("posted_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}?from=all",
            })
            _desc_cache[job_id] = _strip_html(j.get("jd") or "")

        if new_this_page == 0:
            break  # all IDs on this page already seen

        if isinstance(total_expected, int) and len(seen_ids) >= total_expected:
            break  # collected everything the API says exists

        if page_num < _MAX_PAGES:
            time.sleep(0.1)  # be polite between pages

    _job_cache = collected
    print(f"[LatentView] Cache filled: {len(collected)} India jobs")


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
    """Return a slice of LatentView's India postings.

    `keyword`/`location` are accepted for interface compatibility but not
    sent to the API -- the SPA's own requests never send a keyword param
    (same as dbx's own tenant). The full global board is paginated through
    once per process, filtered to India via the `country` field, and
    cached; matcher.py's title/skill filters do the real narrowing.
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
                    f"LatentView cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single LatentView job.

    Served entirely from the cache filled by `fetch_jobs` -- the `alljobs`
    API already returns the full `jd` HTML for every posting in one call,
    so no separate per-job detail request is needed.
    """
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
