"""Fetches Porter job listings via the Darwinbox candidate portal.

Company: Porter (Porter Technologies Pvt Ltd), India's intra-city logistics
platform specialising in last-mile commercial vehicle (mini-truck / tempo)
rentals and on-demand logistics. Founded 2014, headquartered in Bengaluru.
Engineering team focuses on marketplace, routing, pricing, and ML/ops.

ATS discovery (2026-09-26):
  porter.in/careers links to a "SEE OPEN POSITIONS" button that points
  directly to https://porter.darwinbox.in/ms/candidate/careers — the older
  "candidate" SPA product tier, NOT the newer "candidatev2" front-end used by
  Airtel / Darwinbox's own board in this repo.

Cloudflare gating (confirmed 2026-09-26): plain requests.get() (even with a
realistic browser User-Agent) to any endpoint under porter.darwinbox.in
returns HTTP 403 with a Cloudflare "Attention Required!" challenge page. This
is the same platform-wide Cloudflare bot management noted in
clevertap_fetcher.py's investigation. Playwright/Firefox is required to clear
the challenge before the API is accessible — same pattern as darwinbox_fetcher.py
(Darwinbox's own board) in this repo.

API shape (old "candidate" SPA, confirmed against populated sibling Darwinbox
tenants using the same product tier):

  GET /ms/candidateapi/job?page=N&limit=M
    -> {"status": "success",
        "message": {"jobscount": <int total>,
                    "jobs": [{id, title, officelocation_show_arr,
                               tool_tip_locations, job_posting_on, ...}, ...]}}

  GET /ms/candidateapi/job/{id}
    -> {"status": "success",
        "message": {"job": [{id, title, jd (HTML, double-escaped), ...}]}}

Descriptions are NOT inline in the list response (unlike the newer candidatev2
API) — a separate detail call is required per job, also routed through the
Playwright browser context to stay within the Cloudflare-cleared origin.

Location: Darwinbox `candidate` tenant locations are typically already in
"City, State, India" format via `officelocation_show_arr` / `tool_tip_locations`.
If missing, a hardcoded city whitelist normalises bare city names for
is_india_job().

Server-side filtering: no keyword or location param exists on this tenant's
list endpoint (verified against sibling Darwinbox tenants — see also
zomato_fetcher.py, airtel_fetcher.py). The full board is paginated once per
process and cached; matcher.py's title/skill/India filters do the real
narrowing.

Application URL: https://porter.darwinbox.in/ms/candidate/careers/job/{id}
(inferred from the SPA's own routing conventions for the "candidate" tier).

India offices: Bengaluru (HQ), Mumbai, Delhi/NCR.
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

_TENANT = "porter"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidate/careers"
_JOB_LIST_API = f"{_BASE}/ms/candidateapi/job"
_JOB_DETAIL_BASE = f"{_BASE}/ms/candidateapi/job/"
_JOB_PAGE_BASE = f"{_CAREERS_PAGE}/job/"

_PAGE_SIZE = 50
_MAX_PAGES = 40  # safety cap well beyond any plausible board size

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

# GET fetch helper run inside the Playwright page (same-origin → no CORS preflight)
_FETCH_GET_JS = """async (url) => {
    const resp = await fetch(url, {
        method: 'GET',
        credentials: 'include',
        headers: {
            'Accept': 'application/json, text/plain, */*',
            'x-requested-with': 'XMLHttpRequest'
        }
    });
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""

_INDIA_CITIES = (
    "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "chennai",
    "gurugram", "gurgaon", "noida", "delhi", "new delhi", "ahmedabad",
    "kolkata", "jaipur", "chandigarh", "kochi", "trivandrum", "coimbatore",
)


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


def _is_india_city(loc: str) -> bool:
    low = loc.lower()
    return any(city in low for city in _INDIA_CITIES)


def _location_from_job(job: dict) -> str:
    """Extract and normalise location from a Darwinbox candidate API job record.

    Prefers tool_tip_locations (clean list) over officelocation_show_arr
    (may carry stray \\r / trailing airport-code parentheticals on some
    tenants). Falls back to country field, then "India" for any job this
    tenant returns (Porter operates only in India).
    """
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        loc = "; ".join(tips)
        if "india" not in loc.lower() and _is_india_city(loc):
            loc = f"{loc}, India"
        return loc
    raw = (job.get("officelocation_show_arr") or "").replace("\r", "").strip()
    raw = re.sub(r"\s*\([A-Za-z0-9]{2,10}_[A-Za-z0-9]+\)\s*$", "", raw).strip()
    if raw:
        if "india" not in raw.lower() and _is_india_city(raw):
            raw = f"{raw}, India"
        return raw
    return (job.get("country") or "").strip() or "India"


def _ensure_live_context(timeout: int = 30) -> None:
    """Create (or re-create) the Cloudflare-cleared browser context.

    Navigates to the careers page once to clear the Cloudflare challenge and
    stores its cookies in the context. The same page is kept open as
    `_live_page` for all subsequent API calls — the GET requests must originate
    from the same origin (porter.darwinbox.in) to avoid CORS preflight failures
    when x-requested-with is set.
    """
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
    _live_page = page  # kept open deliberately for same-origin API calls


def _call_get_api(url: str, timeout: int = 20) -> dict:
    """GET a JSON API endpoint via the Cloudflare-cleared browser page.

    Raises RateLimitError on HTTP 429, any other non-200 status, or a JSON-
    parse failure after retries are exhausted.
    """
    global _live_context, _live_page
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            result = _live_page.evaluate(_FETCH_GET_JS, url)
            if result["status"] == 429:
                raise RateLimitError(f"Porter: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(
                    f"Porter API {url!r} returned HTTP {result['status']}"
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
                f"Porter API call failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"Porter: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache — paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_cache_filled: bool = False
_FIRST_PAGE_IDS: set[str] | None = None


def _fill_cache(timeout: int = 30) -> None:
    """Paginate through the entire Porter Darwinbox board once and cache it.

    ``_cache_filled`` is set True before the loop so a transient failure does
    not trigger a retry storm on every subsequent fetch_jobs() call in the
    same process (Honeywell/CRED/Razorpay lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled, _job_cache, _FIRST_PAGE_IDS
    if _cache_filled:
        return
    _cache_filled = True

    _ensure_live_context(timeout=timeout)

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None
    total_expected: int | None = None

    for page_num in range(1, _MAX_PAGES + 1):
        url = f"{_JOB_LIST_API}?page={page_num}&limit={_PAGE_SIZE}"
        data = _call_get_api(url, timeout=timeout)

        msg = (data or {}).get("message") or {}
        raw_jobs: list = msg.get("jobs") or []
        if total_expected is None:
            total_expected = msg.get("jobscount")
        if not raw_jobs:
            break

        page_ids = {str(j.get("id") or "") for j in raw_jobs if j.get("id")}

        if page_num == 1:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1 — stop

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
                "posting_date": _epoch_to_date(j.get("job_posting_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}",
            })

        if new_this_page == 0:
            break

        if isinstance(total_expected, int) and len(collected) >= total_expected:
            break

        if page_num < _MAX_PAGES:
            time.sleep(0.1)

    _job_cache = collected
    print(f"[Porter] Cache filled: {len(collected)} total jobs")


# ---------------------------------------------------------------------------
# Public API expected by run_company.py / matcher.py
# ---------------------------------------------------------------------------


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Porter (Darwinbox) postings.

    keyword and location are accepted for interface compatibility but are
    not sent to the API — this tenant's list endpoint ignores every keyword
    param (verified against sibling Darwinbox tenants). The full board is
    paginated through once per process and cached; matcher.py's shared
    title/skill/India filters do the real narrowing.
    """
    if not _cache_filled:
        _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Porter job.

    Descriptions are not included in the Darwinbox candidate list response —
    a separate detail call is made through the Cloudflare-cleared browser
    context. The result is not cached between calls (unlike candidatev2 which
    includes jd inline); the run_company pipeline only calls this for jobs
    that pass title/location filters so the number of calls is small.
    """
    if not _cache_filled:
        _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]

    try:
        data = _call_get_api(f"{_JOB_DETAIL_BASE}{job_id}", timeout=timeout)
    except RateLimitError:
        return "", ""

    job_list = ((data or {}).get("message") or {}).get("job") or []
    detail = job_list[0] if job_list else {}
    description = _strip_html(detail.get("jd") or "")
    posting_date = _epoch_to_date(
        detail.get("posted_on") or detail.get("job_posting_on")
    )
    return description, posting_date
