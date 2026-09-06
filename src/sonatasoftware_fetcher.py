"""Fetches Sonata Software job listings — Darwinbox candidate portal, tenant
"sonataone", Cloudflare-gated.

ATS discovery (2026-09-06): www.sonata-software.com/careers is a Drupal CMS
marketing page that embeds an "Apply Now" button linking to
``/careers/applynow``. That page's custom.js contains a JS redirect:

    if (pathname == '/careers/applynow') {
        window.location.href = "https://sonataone.darwinbox.in/ms/candidate/careers";
    }

So Sonata's real ATS is **Darwinbox**, tenant **"sonataone"**, running the
older "candidate" SPA front-end (``new_careers:false`` — same product tier
as Zomato/Eternal in this repo, NOT the newer "candidatev2" SPA used by
Perfios/Signzy).

Confirmed by inspecting the Angular bundle at
``/ms/candidate/main.38495915aab0be25d160.js`` (fetched without auth):

    apiURL: "/ms/candidateapi/"
    jobList: "job?page="       → GET /ms/candidateapi/job?page=N&limit=M
    jobDetails: "job"          → GET /ms/candidateapi/job/{id}
    jobDropdownValues: "job/filters"
    companyInfo: "companyinfo"

Unlike Zomato's "eternal" tenant (which answers plain ``requests.get()``
with no protection), ``sonataone.darwinbox.in`` sits behind Cloudflare bot
management that returns HTTP 403 to every plain ``requests`` call —
confirmed live (2026-09-06). A real browser (headless Firefox via Playwright)
passes cleanly; this is the same Cloudflare gating as Perfios/BNP Paribas.

API endpoints:

    GET /ms/candidateapi/job?page=N&limit=50
        → {"status":"success","message":{"jobscount":N,"jobs":[...]}}
        Field shapes (same as the Zomato/Eternal Darwinbox client, verified
        against sibling tenants zepto.darwinbox.in / hetero.darwinbox.in):
            id                    : int/str job ID
            title                 : posting title
            officelocation_show_arr: primary location string (may be
                                    "Multiple Locations")
            tool_tip_locations    : list of location strings (used when
                                    officelocation_show_arr says "Multiple")
            job_posting_on        : epoch timestamp (seconds)

    GET /ms/candidateapi/job/{id}
        → {"status":"success","message":{"job":[{"id":...,"jd":...,"posted_on":...}]}}
        jd   : HTML job description (double-HTML-entity-escaped on some
               tenants — unescape → strip tags → unescape again, same as
               Zomato/Razorpay/Groww in this repo)

Server-side filtering:
  - ``keyword``/``q``/``search`` params: tested against sibling Darwinbox
    tenants (zepto.darwinbox.in, hetero.darwinbox.in) — server silently
    ignores them, returning the same full pool regardless (same "ignores
    keywords" finding already in zomato_fetcher / razorpay_fetcher etc.).
    So keywords are IGNORED here; the full board is cached once per process
    and matcher.py's title/skill filters do the real narrowing.
  - ``location`` param: tested against zepto.darwinbox.in with a city name
    — collapsed 41 jobs to 0 (the API likely expects a numeric location ID,
    not a free-text city name). Never sent; wrong value silently zeroes
    real results (same lesson as Zomato: don't guess numeric IDs).

India-job handling: Sonata is an Indian IT services company. All expected
postings are in India. ``officelocation_show_arr`` (and the tooltip list)
typically return "City, State, India" strings directly — ``is_india_job()``
works on them as-is. No location normalisation is needed (unlike some J2W
tenants whose location strings omit the word "India" entirely).

Application URL: ``https://sonataone.darwinbox.in/ms/candidate/careers/job/{id}``
(inferred from the bundle's ``jobDetails:"job"`` route, consistent with how
Zomato's ``zomato_fetcher`` constructs URLs for the same product tier).

Descriptions: NOT inline in the job list. A separate
``GET /ms/candidateapi/job/{id}`` call is required per job. To avoid
re-solving the Cloudflare challenge on every description fetch:
  - The Playwright context used during cache fill is kept alive
    (``_live_context``, module-level) after the page is closed.
  - Description fetches open a new page in the same context (Cloudflare
    cookies already in it) and call ``page.evaluate(fetch(detail_url))``.
  - If the context becomes stale (network error, browser crash), it is
    re-created with a fresh careers-page navigation to re-clear Cloudflare.
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

_TENANT = "sonataone"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidate/careers"
_API_BASE = f"{_BASE}/ms/candidateapi/"
_JOB_LIST_URL = f"{_API_BASE}job"
_JOB_DETAIL_BASE = f"{_API_BASE}job/"
_JOB_PAGE_BASE = f"{_CAREERS_PAGE}/job/"

_PAGE_SIZE = 50
_MAX_PAGES = 60  # safety cap (~3 000 jobs) well beyond any plausible board

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

_FETCH_JS = """async (url) => {
    const resp = await fetch(url, {credentials: 'include'});
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
    global _pw, _browser, _live_context
    try:
        if _live_context:
            _live_context.close()
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
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
    """Prefer the display string; fall back to the tooltip list for
    "Multiple Locations" placeholders — same logic as zomato_fetcher."""
    loc = (job.get("officelocation_show_arr") or "").strip()
    if loc and loc.lower() not in ("multiple locations", "multiple location"):
        return loc
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    return loc or "India"


def _ensure_live_context(timeout: int = 30) -> None:
    """Create (or re-create) the module-level Cloudflare-cleared context.

    Navigates to the careers page once to solve the Cloudflare JS challenge
    and store the ``cf_clearance`` cookie in the context.  Subsequent API
    calls made from pages in this same context are then accepted without
    re-solving the challenge.
    """
    global _live_context
    _ensure_browser()
    if _live_context is not None:
        return  # already cleared, reuse

    ctx = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = ctx.new_page()
    try:
        try:
            page.goto(_CAREERS_PAGE, wait_until="networkidle", timeout=timeout * 1000)
        except PWTimeoutError:
            page.goto(_CAREERS_PAGE, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(4000)
    finally:
        page.close()

    _live_context = ctx


def _call_api(url: str, timeout: int = 20) -> dict:
    """Call a JSON API URL via the Cloudflare-cleared browser context.

    Returns the parsed JSON dict.  Raises ``RateLimitError`` on non-200
    or JSON-parse failure.
    """
    global _live_context
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            page = _live_context.new_page()
            try:
                result = page.evaluate(_FETCH_JS, url)
            finally:
                page.close()

            if result["status"] == 429:
                raise RateLimitError(f"Sonata Software: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(
                    f"Sonata Software API {url!r} returned HTTP {result['status']}"
                )
            return json.loads(result["text"])

        except RateLimitError:
            raise
        except Exception as exc:
            last_exc = exc
            # Context may have gone stale — discard it so _ensure_live_context
            # will re-create it on the next attempt.
            try:
                if _live_context:
                    _live_context.close()
            except Exception:
                pass
            _live_context = None
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Sonata Software API call failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"Sonata Software: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache — paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_cache_filled: bool = False

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


def _fill_cache(timeout: int = 30) -> None:
    """Paginate through the entire Darwinbox board once and cache every job.

    ``_cache_filled`` is set True before the loop so a transient failure
    does not trigger a retry-storm on every subsequent fetch_jobs() call
    in the same process (Honeywell/CRED/Razorpay lesson — PLAYBOOK §Key Bugs).
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

    for page_num in range(1, _MAX_PAGES + 1):
        url = f"{_JOB_LIST_URL}?page={page_num}&limit={_PAGE_SIZE}"
        data = _call_api(url, timeout=timeout)
        raw_jobs: list = ((data or {}).get("message") or {}).get("jobs") or []
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
                "date": _epoch_to_date(j.get("job_posting_on")),
                "url": f"{_JOB_PAGE_BASE}{job_id}",
            })

        if new_this_page == 0:
            # All IDs on this page already seen — wraparound-style dedupe guard.
            break

        if page_num < _MAX_PAGES:
            time.sleep(0.1)  # be polite between pages

    _job_cache = collected
    print(f"[Sonata Software] Cache filled: {len(collected)} total jobs")


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
    """Return a slice of Sonata Software (Darwinbox) postings.

    ``keyword`` and ``location`` are accepted for interface compatibility
    but are not sent to the API — the Darwinbox older-API tenant ignores
    them server-side (verified against sibling tenants; see module docstring).
    The full board is paginated through once per process and cached; all
    narrowing is done by matcher.py's title/skill/India filters.
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
                    f"Sonata Software cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Sonata job.

    Fetches ``/ms/candidateapi/job/{id}`` via the Cloudflare-cleared
    browser context.  The ``jd`` field is HTML-entity-escaped at one extra
    level on some Darwinbox tenants — ``_strip_html`` handles both by
    unescaping, stripping tags, then unescaping again.

    Returns empty strings on parse failure rather than crashing the whole
    pipeline run (same defensive contract used by BNP Paribas / Zomato).
    """
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    detail_url = f"{_JOB_DETAIL_BASE}{job_id}"

    try:
        data = _call_api(detail_url, timeout=timeout)
    except RateLimitError:
        raise
    except Exception as exc:
        raise RateLimitError(
            f"Sonata Software description fetch failed for {job_id}: {exc}"
        ) from exc

    job_list: list = ((data or {}).get("message") or {}).get("job") or []
    detail = job_list[0] if job_list else {}

    description = _strip_html(detail.get("jd") or "")
    posting_date = _epoch_to_date(
        detail.get("posted_on") or detail.get("job_posting_on")
    )
    return description, posting_date
