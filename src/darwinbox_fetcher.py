"""Fetches Darwinbox's OWN job listings — their own careers page on their own
HRMS/ATS platform, tenant "dbx", Cloudflare-gated, candidatev2 SPA.

NOTE: this is Darwinbox the HR-tech COMPANY hiring for itself, not one of the
many other companies in this repo that merely use Darwinbox as their ATS
vendor (zomato/eternal, razorpay, groww, sonatasoftware/sonataone, perfios,
etc. — those are separate tenants with their own fetchers).

ATS discovery (2026-09-06): darwinbox.com/careers is a Next.js marketing page
whose "Explore Careers" button links straight to
``https://dbx.darwinbox.in/ms/candidatev2/main/careers/allJobs`` — Darwinbox's
own tenant, slug **"dbx"**, running the newer "candidatev2" SPA (same product
tier as Perfios/Signzy in this repo, NOT the older "candidate" SPA used by
Zomato/Sonata Software).

Cloudflare gating — confirmed live (2026-09-06): a bare ``requests.post()``
(default ``python-requests/x.y`` User-Agent) to the job-list API gets a flat
HTTP 403 Cloudflare block page. The block is specifically UA-string based on
this tenant (not a full JS-challenge/TLS-fingerprint wall like sonataone/
perfios) — swapping in a realistic browser User-Agent header is enough to get
HTTP 200 back even from plain ``requests``, confirmed reproducible across 5
consecutive fresh-session calls. However, GitHub Actions' shared runner IPs
are known (see other Playwright-based fetchers in this repo, and
``_playwright_startup.py``) to draw materially more aggressive bot-management
treatment from Cloudflare than a residential/dev IP, so — per this repo's
established convention for every other Darwinbox tenant that shows any
Cloudflare signal at all — this fetcher still drives real headless Firefox
via Playwright rather than relying on a plain ``requests`` call that merely
happens to work today from one IP.

Real API endpoint (found via live Playwright network capture of
``/ms/candidatev2/main/careers/allJobs``, then clicking "Load More Jobs" to
observe pagination, then clicking "View and Apply" to confirm the detail
URL):

    POST /ms/candidateapi/job/alljobs?companyId=main
    Body: {"companyId": "main", "page": N, "sort_option": "new", "limit": M}
        -> {"status": "success", "job_counts": <int total>,
            "data": [{id, title, jd (HTML, INLINE — no separate detail call
                      needed, same as Perfios), officelocation_show_arr,
                      tool_tip_locations, country, posted_on, ...}, ...]}

Unlike ``perfios_fetcher.py`` (where a manually re-issued ``fetch()`` call to
the *GET* alljobs endpoint returns HTTP 422 "no job found with that id" and
the natural page-load response has to be captured instead), this tenant's
``alljobs`` endpoint is a **POST** with a JSON body. Replaying it manually
via ``page.evaluate(fetch(url, {method:'POST', body: JSON.stringify(...)}))``
inside the Cloudflare-cleared context works cleanly (HTTP 200, correct
paginated data) — confirmed live — so the simpler sonatasoftware-style
"call the API directly, paginate with a page counter" idiom is used here
rather than Perfios's response-capture-during-natural-load workaround.

Server-side filtering — tested live against the real API:
  - No keyword param exists. ``search``/``keyword``/``q``/``query`` were all
    tried in the POST body; every one is silently ignored — the API always
    returns the same ``job_counts`` total regardless. Keywords are IGNORED
    server-side; matcher.py's title/skill filters do the real narrowing.
  - No location param was found in the SPA's own bundle-driven requests
    (only ``companyId``/``page``/``sort_option``/``limit`` are ever sent by
    the real page). Not attempted/sent here — same "don't guess a param that
    might silently zero real results" lesson as Zomato/Sonata elsewhere in
    this repo.

Pagination: confirmed live by clicking "Load More Jobs" on the real page —
page 1/2/3 return 10 jobs each, a final partial page 4 returns the remainder,
and the response's own ``job_counts`` field gives the true total up front.

Current live state (2026-09-06): 35 total open postings, 18 of them India
(all Hyderabad, Telangana); the rest are US/Singapore/Malaysia/Thailand/
Philippines/UAE roles (Darwinbox's overseas GTM expansion). **Zero of the 18
India postings match the standard title_family keyword set right now** — the
current India openings are Payroll/HCM Implementation, Customer Success,
Sales/Marketing, Legal and Finance roles, plus a "Forward Deployed Engineer"
(builds AI agents/LLM apps per its own JD) and an "AI-Native Systems Builder"
role whose literal titles don't match any configured title_family phrase
("...engineer"/"...developer" family). This is a genuine "zero is a fact"
result, not a fetcher bug — same precedent as Perfios/ING/eClerx elsewhere in
this repo. The pipeline is mechanically correct and will surface a real
engineering opening automatically the moment Darwinbox posts one with a
matching title.

Location quirk: ``officelocation_show_arr`` on this tenant embeds a stray
literal ``\\r`` before ", India" and a trailing airport/city-code parenthetical
(e.g. ``"Hyderabad, Telangana\\r, India (IND_HYD)"``). ``tool_tip_locations``
carries the same information already clean (``"Hyderabad, Telangana, India"``)
— preferred here for exactly that reason.

Description: ``jd`` is HTML-entity-escaped one extra level (raw text starts
``&lt;p ...&gt;``), same idiom as Zomato/Razorpay/Groww/Perfios/Sonata in this
repo — unescape, strip tags, unescape again. Already inline in the list
response, so ``fetch_job_description`` is served entirely from the cache
filled by ``fetch_jobs`` — no extra per-job API call, same as Perfios.

Application URL: ``https://dbx.darwinbox.in/ms/candidatev2/main/careers/
jobDetails/{id}?from=all`` — confirmed live via Playwright click-through on
a real "View and Apply" link.
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

_TENANT = "dbx"
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
    """Prefer the clean tooltip list — ``officelocation_show_arr`` on this
    tenant embeds a stray literal "\\r" and a trailing city-code
    parenthetical (e.g. "Hyderabad, Telangana\\r, India (IND_HYD)") that
    ``tool_tip_locations`` doesn't carry."""
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    raw = (job.get("officelocation_show_arr") or "").replace("\r", "").strip()
    raw = _CODE_SUFFIX_RE.sub("", raw).strip()
    return raw or (job.get("country") or "").strip() or "India"


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
    actually navigated to ``dbx.darwinbox.in`` succeeds immediately, because
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
                raise RateLimitError(f"Darwinbox: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(
                    f"Darwinbox API {url!r} returned HTTP {result['status']}"
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
                f"Darwinbox API call failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"Darwinbox: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache — paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


def _fill_cache(timeout: int = 30) -> None:
    """Paginate through the entire Darwinbox board once and cache every job.

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

    _job_cache = collected
    print(f"[Darwinbox] Cache filled: {len(collected)} total jobs")


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
    """Return a slice of Darwinbox's own postings.

    ``keyword`` and ``location`` are accepted for interface compatibility
    but are not sent to the API — this tenant's ``alljobs`` endpoint ignores
    every keyword param tried (verified live; see module docstring) and no
    location param exists in the real SPA's own requests. The full board is
    paginated through once per process and cached; all narrowing is done by
    matcher.py's title/skill/India filters.
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
                    f"Darwinbox cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Darwinbox job.

    Served entirely from the cache filled by ``fetch_jobs`` — the ``alljobs``
    API already returns the full ``jd`` HTML for every posting in one call,
    so no separate per-job detail request is needed (same as Perfios).
    """
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
