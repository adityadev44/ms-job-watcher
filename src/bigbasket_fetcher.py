"""BigBasket job fetcher — Darwinbox candidate portal, tenant "bigbasket",
behind Cloudflare Bot Management (Playwright required, unlike Zomato's
open "eternal" tenant on the same product).

ATS discovery (2026-09-14): careers.bigbasket.com's own page is a static
marketing page whose one "View jobs" / apply-flow link points at
`https://bigbasket.darwinbox.in/ms/candidate/careers` -- the same Darwinbox
product already integrated for Zomato (`eternal` tenant), Zepto, and Hetero
elsewhere in this repo. An older `careers.bigbasket.com/jobs/{id}` URL shape
found via web search is dead -- it now redirects to an S3 bucket that
returns a raw `<Error><Code>AccessDenied</Code>...` XML body, confirming the
legacy "BB-Careers" portal referenced in old job postings no longer exists;
Darwinbox is the current, live system.

**Unlike Zomato's tenant, this one is Cloudflare Bot-Management gated at the
API layer.** A cold `requests.get` (or even a cold Playwright-Chromium
`fetch()` issued immediately after page load) to
`/ms/candidateapi/job` returns HTTP 403 with a Cloudflare "Attention
Required!" challenge page and a fresh `__cf_bm` cookie -- reusing that
`__cf_bm` cookie value from a real page load in a *separate* plain-`requests`
call still 403s (confirmed live), which means this is a TLS/JA3 or
behavioral fingerprint check, not just a missing cookie -- so every API call
must happen from inside the same live browser page via `page.evaluate`
(`fetch()` executed in-page), not a plain HTTP client, even with cookies
copied out.

**Key finding: an 8-second warm-up after page load is enough for Cloudflare
to grant the bot-management token.** A `fetch()` issued immediately after
`page.goto()` still 403s; the identical call issued after `time.sleep(8)`
returns HTTP 200 with real JSON, confirmed reproducibly across multiple
separate runs. This fetcher warms up once per browser-context lifetime (not
per call) and reuses the same page/context for every subsequent
`page.evaluate` fetch, since the underlying `__cf_bm` token is
context-scoped and short-lived cookie renewal happens transparently on
each same-context call.

API shape is otherwise identical to `zomato_fetcher.py` (same Darwinbox
product, different tenant):
    GET /ms/candidateapi/job?page=N&limit=M   -> job list (paginated)
    GET /ms/candidateapi/job/{id}             -> job detail (`jd` field)
No auth/session token beyond the Cloudflare-granted cookie; keyword/location
params are ignored server-side (verified against the populated Zepto/Hetero
tenants for the same product in `zomato_fetcher.py`), so the whole small
board is cached once per process.

**Current live state (2026-09-14): 5 total open postings, zero software
engineering titles.** All 5 are ops/HR/recruiting roles (EXECUTIVE -
RECRUITER, CORPORATE ALLIANCE EXECUTIVE - HORECA, SENIOR EXECUTIVE - HR, HUB
MANAGER, ORDER PICK STATION) across warehouse/hub locations (Kolkata,
Bhubaneswar, Guwahati, Ranchi, Hyderabad) -- a real, live, working pipeline
with a genuinely non-engineering current pool, not a fetcher bug. Onboarded
anyway per the Zomato/Volvo Cars/Sanofi precedent: the mechanism is proven
end-to-end (5 real jobs round-tripped through list + detail calls), and any
future BigBasket engineering requisition posted to this same tenant will be
picked up automatically with no code change.

Location quirk (identical to Zomato's): `officelocation_show_arr` is the
literal string "Multiple locations" for every current posting; the real
per-site list lives in `tool_tip_locations`, joined with "; " the same way
`zomato_fetcher.py::_location_from_job` does.

Application URL is a best-effort construction
(`https://bigbasket.darwinbox.in/ms/candidate/careers/job/{id}`), the same
route-name-derived, not click-verified, gap flagged in `zomato_fetcher.py`
-- there is no way to click through a real "Apply" flow when zero of the
current 5 postings are software roles to test with either.
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

_TENANT = "bigbasket"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidate/careers"
_JOB_LIST_PATH = f"{_BASE}/ms/candidateapi/job"
_JOB_DETAIL_BASE = f"{_BASE}/ms/candidateapi/job/"
_JOB_PAGE_BASE = f"{_CAREERS_PAGE}/job/"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_PAGE_SIZE = 50
_MAX_PAGES = 40  # safety cap, well beyond this small tenant's plausible size
_WARMUP_SECONDS = 8  # empirically the minimum time for Cloudflare's __cf_bm
# bot-management token to be granted to this browser context (see module
# docstring) -- a fetch issued immediately after page load still 403s.


class RateLimitError(Exception):
    """Raised on persistent failure or when Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton -- Chromium (Cloudflare bot-management token acquisition
# was verified working under Chromium; not cross-checked against Firefox).
# ---------------------------------------------------------------------------

_pw = None
_browser = None
_context = None
_page = None
_warmed_up = False


def _ensure_page():
    global _pw, _browser, _context, _page, _warmed_up
    if not _PLAYWRIGHT_AVAILABLE:
        raise RateLimitError(
            "playwright not installed — run: pip install playwright && "
            "playwright install chromium"
        )
    if _page is not None:
        return _page

    with STARTUP_LOCK:
        _pw = sync_playwright().start()
        try:
            _browser = _pw.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"]
            )
            _context = _browser.new_context(user_agent=_UA)
            _page = _context.new_page()
        except Exception:
            if _pw is not None:
                _pw.stop()
            _pw = None
            _browser = None
            _context = None
            _page = None
            raise
        atexit.register(_shutdown_browser)

    try:
        _page.goto(_CAREERS_PAGE, wait_until="load", timeout=30000)
    except PWTimeoutError:
        pass
    time.sleep(_WARMUP_SECONDS)
    _warmed_up = True
    return _page


def _shutdown_browser() -> None:
    global _pw, _browser, _context, _page
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = None
    _browser = None
    _context = None
    _page = None


def _api_get_json(url: str, context_label: str) -> dict:
    """Issue a same-context in-page fetch() so Cloudflare's bot-management
    token (bound to this browser's TLS/behavioral fingerprint, not just a
    cookie value) is honored -- a plain requests.get with the same cookie
    copied out still 403s (confirmed live, see module docstring)."""
    page = _ensure_page()
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            result = page.evaluate(
                """
                async (url) => {
                    const r = await fetch(url, {headers: {'Accept': 'application/json'}});
                    const text = await r.text();
                    return {status: r.status, body: text};
                }
                """,
                url,
            )
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"BigBasket {context_label}: page.evaluate failed — {exc}") from exc

        status = result.get("status")
        if status == 200:
            try:
                return json.loads(result.get("body") or "{}")
            except ValueError as exc:
                raise RateLimitError(f"BigBasket {context_label}: invalid JSON — {exc}") from exc
        if status == 429:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"BigBasket {context_label}: 429 rate-limited")
        # Cloudflare challenge (403) or other transient failure — retry once
        # with a fresh warm-up delay before giving up.
        if attempt < 2:
            time.sleep(3)
            continue
        raise RateLimitError(f"BigBasket {context_label}: HTTP {status} after retries")

    raise RateLimitError(f"BigBasket {context_label}: no response — {last_exc}")


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


_job_cache: list[dict] = []
_detail_cache: dict[str, dict] = {}
_cache_filled: bool = False


def _fill_cache() -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    for page_num in range(1, _MAX_PAGES + 1):
        data = _api_get_json(
            f"{_JOB_LIST_PATH}?page={page_num}&limit={_PAGE_SIZE}",
            context_label=f"job list page {page_num}",
        )
        raw_jobs = ((data or {}).get("message") or {}).get("jobs") or []
        if not raw_jobs:
            break
        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or j.get("designation_name") or "").strip()
            if not (job_id and title):
                continue
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": _epoch_to_date(j.get("job_posting_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}",
            })

    _job_cache[:] = collected
    print(f"[BigBasket] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of BigBasket (Darwinbox "bigbasket" tenant) postings.

    keyword/location are accepted for interface compatibility but ignored
    server-side (same product/behavior as zomato_fetcher.py); the whole
    small board is cached once per process behind the Cloudflare warm-up.
    """
    _fill_cache()
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    return application_url.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single BigBasket job."""
    job_id = _job_id_from_url(application_url)

    detail = _detail_cache.get(job_id)
    if detail is None:
        data = _api_get_json(
            f"{_JOB_DETAIL_BASE}{job_id}",
            context_label=f"job detail {job_id}",
        )
        job_list = ((data or {}).get("message") or {}).get("job") or []
        detail = job_list[0] if job_list else {}
        _detail_cache[job_id] = detail

    description = _strip_html(detail.get("jd") or "")
    posting_date = _epoch_to_date(detail.get("posted_on") or detail.get("job_posting_on"))
    return description, posting_date
