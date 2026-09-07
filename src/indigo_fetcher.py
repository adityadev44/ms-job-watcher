"""Fetches IndiGo / InterGlobe Aviation job listings from its own custom
careers site (www.goindigo.in/careers), backed by SAP SuccessFactors
Recruiting under the hood.

ATS discovery (live, 2026-09-07): www.goindigo.in/careers/job-search.html is
IndiGo's OWN in-house-branded career site (Adobe AEM-served static shell,
``skypluscareers`` clientlib) -- NOT a vendor careers-page skin. A live
network-request capture (Playwright) shows the page's own JS calling a
company-owned middleware host:

    GET https://ms-careers-prod.goindigo.in/career-job-list
    GET https://ms-careers-prod.goindigo.in/career-job-details?jobReqId=<id>
    GET https://ms-careers-prod.goindigo.in/career-department-list
    GET https://ms-careers-prod.goindigo.in/career-location-list

which in turn proxies SAP SuccessFactors Recruiting OData (every job's
``__metadata.uri`` is a literal ``https://api-in10.hr.cloud.sap/odata/v2/
JobRequisition(<id>L)`` URL) -- so this is IndiGo's own SuccessFactors
tenant, wrapped in a bespoke JSON API rather than exposed as SF's usual
public J2W career site. Both endpoints require a static header
``user_key: 03ba3c0795ce04ed48e7fe3854155a1f`` (confirmed identical across
multiple independent page loads -- a fixed app key baked into the SPA
bundle, not a per-session token) plus ``Origin/Referer: goindigo.in``.

Bot-management gating -- confirmed live, and the one genuinely tricky part
of this fetcher:
  - Plain ``requests`` (any UA/headers) never completes: the TLS handshake
    stalls indefinitely over IPv6, and forcing IPv4 gets an HTTP/2 stream
    reset (``INTERNAL_ERROR``) immediately after headers are sent, on
    *every* path on the domain including the plain homepage. This is
    Akamai Bot Manager (cert issuer chain confirms Akamai; failover assets
    are served from ``/akamfailoverpage/``), not a page-specific block.
  - Headless **Chromium** via Playwright gets a clean HTTP 200 on every
    URL tried (careers page AND bare homepage) but the *body* is always
    Akamai's static ~1KB soft-fail page ("Something went wrong. Please
    contact our customer support team.") -- Akamai's bot-management JS
    sensor is specifically fingerprinting headless Chromium and serving a
    fake-200 failover rather than a hard block.
  - Headless **Firefox** via Playwright gets the real, full page (300+KB)
    on every attempt (confirmed across 4 separate fresh-browser runs) --
    same evasion already relied on by darwinbox_fetcher.py/
    sonatasoftware_fetcher.py for Cloudflare in this repo, just against a
    different vendor's bot management here. ``ms-careers-prod.goindigo.in``
    itself is reachable via plain ``page.evaluate(fetch(...))`` from
    *within* a Firefox context that has already navigated to
    goindigo.in once (this establishes whatever cookie/fingerprint Akamai
    is checking) -- no direct API access is possible from a fresh browser
    context or from plain `requests`, confirmed live. The fetch call must
    NOT set ``credentials: 'include'`` -- confirmed live that this tenant's
    CORS response for this cross-subdomain call has no
    ``Access-Control-Allow-Credentials`` header, so adding ``include``
    makes the browser reject every response with a bare "NetworkError"
    (the default omitted/``same-origin`` credentials mode works fine).

Server-side filtering -- tested live against the real API and against the
rendered page's own search box:
  - ``career-job-list`` takes NO query params at all: a nonsense
    ``?search=zzznonsense123`` suffix returns byte-for-byte the same
    164027-char response as no params. The page's own "Search by job
    title"/"Location" boxes are confirmed (live network capture) to fire
    ZERO additional requests when typed into -- "Engineer" narrows the
    on-page result count from 18 to 5 with no XHR at all, i.e. pure
    client-side JS filtering over a single unfiltered payload already in
    the browser. Keywords/location are therefore not sent to the API here
    either -- matcher.py's shared title/skill/India filters do the real
    narrowing, same reasoning as airindia_fetcher.py/lufthansa_fetcher.py
    for a small board.
  - The whole current board is tiny (18 open reqs total, live 2026-09-07)
    and every single one is already India-based (Pan-India/Gurgaon/Mumbai/
    Chandigarh -- confirmed by listing every ``location_obj`` on the live
    board; IndiGo has no overseas postings on this tenant at all right
    now), so the whole board is cached once per process rather than
    re-fetched per keyword.

Location: ``location_obj.results[].name`` is a bare city name (e.g.
"Gurgaon") or the literal "Pan-India" (which already contains "india" and
passes the shared India filter unchanged). Known city names are suffixed
with ", India" so the shared filter's ``"india" in location.lower()`` check
matches consistently; anything unrecognized is left unchanged rather than
guessed.

Posting date: each job carries 2-3 ``jobReqPostings`` entries, one per
board (``_internal``, ``_private_external``, ``_external``) with its own
``postStartDate`` (``/Date(<epoch_ms>)/`` .NET-style wire format, a
SuccessFactors OData convention). The ``_external`` board's
``postStartDate`` -- the date it actually went public on this careers
site -- is used; all 18 live postings carry an ``_external`` entry.

Description: ``externalJobDescription`` (full HTML) is already inline in
the SAME ``career-job-list`` response used to build the list -- no separate
per-job detail call is needed (same "cache-once, already inline" shape as
darwinbox_fetcher.py/perfios_fetcher.py). ``fetch_job_description`` is
served entirely from the module-level cache.

Application URL: ``https://www.goindigo.in/careers/job-details/<slug>/
<id>.html`` -- confirmed live that the ``<slug>`` segment is purely
cosmetic (a request with an arbitrary garbage slug and the real numeric ID
still renders the correct job), so a best-effort slug is built from the
title for a human-readable URL and ``fetch_job_description`` parses the
trailing numeric ID back out regardless of what slug text precedes it.
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

_BASE = "https://www.goindigo.in"
_SEARCH_PAGE = f"{_BASE}/careers/job-search.html"
_JOB_LIST_API = "https://ms-careers-prod.goindigo.in/career-job-list"
_JOB_DETAIL_BASE = f"{_BASE}/careers/job-details/"

_USER_KEY = "03ba3c0795ce04ed48e7fe3854155a1f"
_PAGE_SIZE = 25  # whole board is cached in one shot; not a real page size

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

_FETCH_JS = """async (args) => {
    const [url, userKey] = args;
    const resp = await fetch(url, {
        headers: {'user_key': userKey}
    });
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""

_INDIA_CITIES = {
    "mumbai", "bengaluru", "bangalore", "gurugram", "gurgaon", "delhi",
    "new delhi", "hyderabad", "chennai", "pune", "kolkata", "kochi",
    "ahmedabad", "chandigarh", "goa", "lucknow", "jaipur", "coimbatore",
    "nagpur", "indore", "guwahati", "patna", "bhubaneswar", "amritsar",
    "varanasi", "raipur", "ranchi", "srinagar", "trichy", "vadodara",
    "surat", "kannur", "kozhikode", "trivandrum", "thiruvananthapuram",
    "vijayawada", "visakhapatnam", "madurai", "dehradun",
}


class RateLimitError(Exception):
    """Raised when the portal is unreachable, blocked, or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox required (Akamai Bot Manager fingerprints
# headless Chromium on this tenant; plain requests never completes at all)
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


def _ensure_live_context(timeout: int = 30) -> None:
    """Navigate a Firefox context to the careers page once so subsequent
    ``page.evaluate(fetch(...))`` calls to ms-careers-prod.goindigo.in are
    accepted by Akamai (a fresh/blank context is not enough — confirmed
    live, same-origin navigation first is required)."""
    global _live_context, _live_page
    _ensure_browser()
    if _live_context is not None and _live_page is not None:
        return

    ctx = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = ctx.new_page()
    try:
        page.goto(_SEARCH_PAGE, wait_until="networkidle", timeout=timeout * 1000)
    except PWTimeoutError:
        page.goto(_SEARCH_PAGE, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(4000)

    _live_context = ctx
    _live_page = page


def _call_api(url: str, timeout: int = 20) -> dict:
    """GET an API URL via the Akamai-cleared Firefox page and return the
    parsed JSON dict. Raises RateLimitError on HTTP 429, any other
    non-200 status, or a JSON-parse failure after retries are exhausted."""
    global _live_context, _live_page
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            result = _live_page.evaluate(_FETCH_JS, [url, _USER_KEY])

            if result["status"] == 429:
                raise RateLimitError(f"IndiGo: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(f"IndiGo API {url!r} returned HTTP {result['status']}")
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
            raise RateLimitError(f"IndiGo API call failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"IndiGo: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


_DATE_RE = re.compile(r"/Date\((\d+)\)/")


def _parse_wire_date(raw: str) -> int | None:
    m = _DATE_RE.search(raw or "")
    return int(m.group(1)) if m else None


def _epoch_ms_to_date(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def _external_posting_date(job_raw: dict) -> str:
    postings = (job_raw.get("jobReqPostings") or {}).get("results") or []
    dates = [
        _parse_wire_date(p.get("postStartDate"))
        for p in postings
        if p.get("boardId") == "_external"
    ]
    dates = [d for d in dates if d]
    if not dates:
        return ""
    return _epoch_ms_to_date(max(dates))


def _normalise_location(names: list[str]) -> str:
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return "India"
    out = []
    for name in cleaned:
        if re.search(r"\bindia\b", name, re.IGNORECASE):
            out.append(name)
        elif name.strip().casefold() in _INDIA_CITIES:
            out.append(f"{name}, India")
        else:
            out.append(name)
    return "; ".join(out)


_SLUG_RE = re.compile(r"[^A-Za-z0-9()]+")


def _slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.strip()).strip("-")
    return slug or "job"


_JOB_ID_RE = re.compile(r"/(\d+)\.html?$")


# ---------------------------------------------------------------------------
# Job-list cache — single unfiltered call per process (see module docstring)
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False
_cache_error: RateLimitError | None = None


def _fill_cache(timeout: int = 30) -> None:
    """Fetch the whole board once and cache every job.

    ``_cache_filled`` is set (and any failure latched into ``_cache_error``)
    before/during the one real API call so a failed fill never silently
    turns into an empty-but-"successful" cache on a later call in the same
    process (the Honeywell/Persistent lesson — see airindia_fetcher.py).
    """
    global _cache_filled, _job_cache, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    try:
        _ensure_live_context(timeout=timeout)
        data = _call_api(_JOB_LIST_API, timeout=timeout)
    except RateLimitError as exc:
        _cache_error = exc
        raise
    except Exception as exc:
        _cache_error = RateLimitError(f"IndiGo cache fill failed: {exc}")
        raise _cache_error from exc

    if not isinstance(data, dict) or "result" not in data:
        _cache_error = RateLimitError("IndiGo: career-job-list response missing 'result'")
        raise _cache_error

    raw_jobs: list = data.get("result") or []
    collected: list[dict] = []

    for j in raw_jobs:
        job_id = str(j.get("jobReqId") or "").strip()
        if not job_id:
            continue

        status_codes = {
            s.get("externalCode")
            for s in (j.get("status") or {}).get("results") or []
        }
        if status_codes and "Open" not in status_codes:
            continue

        locale = (j.get("jobReqLocale") or {}).get("results") or []
        title = (locale[0].get("externalTitle") or "").strip() if locale else ""
        if not title:
            continue

        loc_names = [
            l.get("name") for l in (j.get("location_obj") or {}).get("results") or []
        ]
        location = _normalise_location(loc_names)

        posting_date = _external_posting_date(j)
        application_url = f"{_JOB_DETAIL_BASE}{_slugify(title)}/{job_id}.html"

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": posting_date,
            "application_url": application_url,
        })
        _desc_cache[job_id] = _strip_html(
            locale[0].get("externalJobDescription") or ""
        ) if locale else ""

    _job_cache = collected
    print(f"[IndiGo] Cache filled: {len(collected)} total jobs")


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
    """Return a slice of IndiGo's current open postings.

    ``keyword``/``location`` are accepted for interface compatibility but
    not sent to the API — this board's own search box does the same
    filtering entirely client-side over a single unfiltered payload (see
    module docstring), so the whole board (~18 postings) is cached once
    per process and matcher.py's shared filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single IndiGo job.

    Served entirely from the cache filled by ``fetch_jobs`` — the
    ``career-job-list`` API already returns each job's full
    ``externalJobDescription`` HTML inline, so no separate per-job detail
    call is needed.
    """
    m = _JOB_ID_RE.search(application_url)
    job_id = m.group(1) if m else ""
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
