"""Fetches Ashok Leyland (Chennai-headquartered Indian truck/bus
manufacturer) job listings via its own Darwinbox tenant.

ATS discovery (live, 2026-09-13/14): ashokleyland.com's homepage links to
``/in/careers``, whose "View All Jobs" button links straight to
``https://ashokleyland.darwinbox.in/ms/candidatev2/a61cb038c35a54/careers/
allJobs`` -- Darwinbox, tenant id ``a61cb038c35a54``, the newer
"candidatev2" SPA (same product tier as Darwinbox's own careers page and
Perfios/Signzy in this repo, NOT the older "candidate" SPA used by Zomato/
Sonata Software). This is a DIFFERENT tenant from every other Darwinbox
company already in this repo -- not a shared pool.

Cloudflare gating -- confirmed live: a bare ``requests.post()`` to
``/ms/candidateapi/job/alljobs`` gets a flat HTTP 403 Cloudflare
"Attention Required!" block page (this tenant's Cloudflare rule is stricter
than darwinbox.com's own UA-string-only gate — a realistic browser
User-Agent alone is NOT enough here, confirmed via plain ``requests``).
Per this repo's established convention for this class of tenant
(honeywell/ibm/natwest/perfios/servicenow/sonatasoftware/darwinbox/etc.),
this fetcher drives headless Firefox via Playwright, reusing the SAME page
that navigated to the careers SPA for all subsequent same-origin API calls
(a blank ``about:blank`` page can't satisfy this endpoint's CORS preflight
for a JSON POST -- same gotcha documented in darwinbox_fetcher.py).

Real API (same shape as darwinbox_fetcher.py, different tenant/company id):

    POST /ms/candidateapi/job/alljobs?companyId=a61cb038c35a54
    Body: {"companyId": "a61cb038c35a54", "page": N, "sort_option": "new",
           "limit": M}
        -> {"status": "success", "job_counts": <int total>,
            "data": [{id, title, jd (HTML, INLINE), officelocation_show_arr,
                      tool_tip_locations, country, posted_on, ...}, ...]}

Server-side filtering -- tested live: no keyword param exists (same as
Darwinbox's own tenant); every page returns the same ``job_counts`` total
regardless of any search param tried. Location: no filter param either
--this whole (small) global pool is India already (India-only manufacturer,
no overseas listings observed).

Current live state (investigation time): only 18 total open postings
company-wide, ALL India, spanning Ennore/Chennai (Tamil Nadu, excluded),
Pantnagar (Uttarakhand), Alwar (Rajasthan), Gandhidham/Ahmedabad/Surat
(Gujarat), Pune (Maharashtra, excluded), Ranchi (Jharkhand), Jabalpur/
Bhopal/Katni (Madhya Pradesh), Jagdalpur (Chhattisgarh) -- explicitly NOT
Chennai-only, since most postings are outside Chennai/Pune. Current titles
are Sales/Production/Maintenance/Legal/Safety roles with zero .NET/C#/AI/ML
primary-skill matches right now -- a genuine current-zero-match snapshot
(same "real, working, low-volume board -- included for completeness" call
as Ford/General Motors/BNY Mellon/Icertis elsewhere in this repo), NOT a
structural zero: external listings (Glassdoor/Naukri/LinkedIn) confirm
Ashok Leyland does post Embedded Software Engineer / System Integration
Engineer (ADAS) roles from Chennai and other sites from time to time: this
pipeline will surface them automatically the moment they appear on this
same board.

Location: ``tool_tip_locations`` (a list of clean "City, State, India"
strings, sometimes >1 entry for a multi-site posting) is used, same
preference as darwinbox_fetcher.py, over the noisier
``officelocation_show_arr`` (embeds trailing office-code parentheticals).

Description: ``jd`` is HTML-entity-escaped HTML, inline in the list
response -- no separate per-job detail call needed, same as
darwinbox_fetcher.py/Perfios.

Application URL: ``https://ashokleyland.darwinbox.in/ms/candidatev2/
a61cb038c35a54/careers/jobDetails/{id}`` -- confirmed live via Playwright
link-harvesting on the real allJobs page (no ``?from=all`` suffix on this
tenant, unlike Darwinbox's own).
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

_TENANT = "ashokleyland"
_COMPANY_ID = "a61cb038c35a54"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidatev2/{_COMPANY_ID}/careers/allJobs"
_ALLJOBS_API = f"{_BASE}/ms/candidateapi/job/alljobs?companyId={_COMPANY_ID}"
_JOB_PAGE_BASE = f"{_BASE}/ms/candidatev2/{_COMPANY_ID}/careers/jobDetails/"

_PAGE_SIZE = 50
_MAX_PAGES = 10  # safety cap (~500 jobs), well beyond this board's ~18-job size

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


_CODE_SUFFIX_RE = re.compile(r"\s*\([A-Za-z0-9]{4,}\)\s*$")


def _location_from_job(job: dict) -> str:
    """Prefer the clean tooltip list over the noisier
    ``officelocation_show_arr`` (carries trailing office-code
    parentheticals, e.g. "Chennai Alcob, Chennai, Tamil Nadu, India
    (10201025)") -- same preference as darwinbox_fetcher.py."""
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    raw = (job.get("officelocation_show_arr") or "").replace("\r", "").strip()
    raw = _CODE_SUFFIX_RE.sub("", raw).strip()
    return raw or (job.get("country") or "").strip() or "India"


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


def _call_api(url: str, body: dict, timeout: int = 20) -> dict:
    global _live_context, _live_page
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            result = _live_page.evaluate(_FETCH_JS, [url, body])

            if result["status"] == 429:
                raise RateLimitError(f"Ashok Leyland: 429 rate-limited from {url}")
            if result["status"] != 200:
                raise RateLimitError(
                    f"Ashok Leyland API {url!r} returned HTTP {result['status']}"
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
                f"Ashok Leyland API call failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"Ashok Leyland: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Job-list cache -- paginate once per process, serve slices per keyword call
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False
_FIRST_PAGE_IDS: set[str] | None = None


def _fill_cache(timeout: int = 30) -> None:
    global _cache_filled, _job_cache, _FIRST_PAGE_IDS
    if _cache_filled:
        return
    _cache_filled = True  # set before the loop — avoid retry storms

    _ensure_live_context(timeout=timeout)

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None
    total_expected: int | None = None

    for page_num in range(1, _MAX_PAGES + 1):
        body = {
            "companyId": _COMPANY_ID,
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
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break

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
                "application_url": f"{_JOB_PAGE_BASE}{job_id}",
            })
            _desc_cache[job_id] = _strip_html(j.get("jd") or "")

        if new_this_page == 0:
            break

        if isinstance(total_expected, int) and len(collected) >= total_expected:
            break

        if page_num < _MAX_PAGES:
            time.sleep(0.1)

    _job_cache = collected
    print(f"[Ashok Leyland] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a slice of Ashok Leyland's own postings.

    ``keyword``/``location`` are accepted for interface compatibility but
    are not sent to the API -- no keyword/location param exists on this
    tenant (verified live; see module docstring). The full (small) board is
    paginated through once per process and cached; matcher.py's title/
    skill/India filters do the real narrowing.
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
                    f"Ashok Leyland cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Ashok Leyland job.

    Served entirely from the cache filled by ``fetch_jobs`` -- the
    ``alljobs`` API already returns the full ``jd`` HTML for every posting
    in one call, same as darwinbox_fetcher.py/Perfios.
    """
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
