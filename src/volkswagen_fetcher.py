"""Fetches Volkswagen job listings — Volkswagen Group Technology/Digital
Solutions India, via its SAP SuccessFactors "Career Site" (an older,
DWR-based flavor, distinct from every other SuccessFactors integration in
this repo).

**Re-verifying the earlier-flagged "Pune-only" exclusion (this task's
specific ask):** an earlier onboarding wave flagged "VW Group Technology
Solutions" as a structural-zero Pune-only GCC and excluded it without a live
recheck. Re-investigated from scratch on 2026-09-14: the entity's own
current marketing site (`vwg-digitalsolutions.in`, "Volkswagen Group Digital
Solutions [India]") states **4,425+ employees across 4 offices: Pune,
Bengaluru, Gurugram, and Prague** — i.e. it is explicitly NOT Pune-only, and
in fact spans 3 Indian cities. The earlier flag was wrong (or the
organization's footprint changed since); this fetcher is onboarded on the
strength of that correction, per this repo's own standing "re-verify, don't
assume" lesson (see the Qualcomm/AB InBev/Novartis/etc. entry in
"Key Bugs We Hit").

**ATS discovery:** the marketing site's "Apply For Job Opportunities"
button fires `window.open('https://career10.successfactors.com/career?
company=volkswag04&site=VjItRnhuMkZIVFNnOWZ0c1djNGhmTjlZdz09')` — an older
SAP SuccessFactors "Career Site" product (not Job2Web classic, not J2W
Unify, not Career Site Builder/CSB2 — a fourth distinct SuccessFactors
career-site flavor for this repo) built on **DWR** (Direct Web Remoting), a
Java/JS RPC bridge that predates both other SF families already documented
here. The real search call, found via live Playwright network capture:

    POST /xi/ajax/remoting/call/plaincall/
         careerJobSearchControllerProxy.getInitialJobSearchData.dwr
        -> a `.dwr`-encoded JS-literal response (NOT JSON) whose
           `s2.postings` array holds `{id, title, postingDate}` per job

**Why Playwright is required for search but not for job detail pages:**
this DWR endpoint pairs a server-side `scriptSessionId` with the request's
`JSESSIONID` cookie in a way a cold `requests` session can't replicate
(hand-copying a `scriptSessionId` string from one page load into a
follow-up plain POST reproducibly 403s "not authorized to access the
functionality you have requested", even with a fresh matching
`JSESSIONID`) — only a real browser executing the page's own DWR JS
generates a session pairing the server accepts. Job **detail** pages,
however (`/career?career_ns=job_listing&company=volkswag04&...&
career_job_req_id={id}`), are fully server-rendered HTML needing no session
at all — confirmed with a cold, cookie-less `requests.get()`.

**No location field exists anywhere in this data source** — neither the DWR
search response's per-posting object nor the server-rendered detail page
exposes a city/location field (`otherValues` in the DWR payload, where a
configured location field would normally appear, comes back empty for every
posting on this tenant; the rendered detail page's own body text never
names a city either). This specific career site (reached only via the
India-specific `vwg-digitalsolutions.in` marketing page, distinct from
Volkswagen AG's global career site) is corroborated as India-scoped by the
one posting that DOES mention a country in its JD body ("...join our team
in **India**...", requisition 10513) and by the complete absence of any
Prague/Czech mention across every posting sampled — so every job is treated
as a bare `"India"` location. **This is a known, accepted precision gap**
(documented the same way as `deltatre_fetcher.py`'s identical "location is
always a bare country string, no city" limitation): a hypothetical
Pune-based posting on this specific tenant would NOT be caught by
`exclude_locations`, since there is no city text anywhere to match against.
Real, current postings sampled (Sr. Developer, Technical Expert, SAP
Expert, Manager: IT Infra, Data Center Operator, Product Sr. Manager) are
genuine IT/software-adjacent titles — a working, non-Pune-only pipeline
worth the accepted gap, same call this repo already made for Deltatre.
"""

from __future__ import annotations

import atexit
import re

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_COMPANY = "volkswag04"
_SITE = "VjItRnhuMkZIVFNnOWZ0c1djNGhmTjlZdz09"
_BASE = "https://career10.successfactors.com"
_LISTING_URL = (
    f"{_BASE}/career?company={_COMPANY}&career_ns=job_listing_summary"
    f"&navBarLevel=JOB_SEARCH&site={_SITE}"
)
_DETAIL_URL = (
    f"{_BASE}/career?career_ns=job_listing&company={_COMPANY}"
    f"&navBarLevel=JOB_SEARCH&rcm_site_locale=en_GB&site={_SITE}"
    "&career_job_req_id={job_id}"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept": "text/html"}


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


_pw = None
_browser = None


def _ensure_browser() -> None:
    global _pw, _browser
    if not _PLAYWRIGHT_AVAILABLE:
        raise RateLimitError(
            "playwright not installed — run: pip install playwright && "
            "playwright install chromium"
        )
    if _browser is None:
        with STARTUP_LOCK:
            _pw = sync_playwright().start()
            try:
                _browser = _pw.chromium.launch(headless=True)
            except Exception:
                _pw.stop()
                _pw = None
                raise
        atexit.register(_shutdown_browser)


def _shutdown_browser() -> None:
    global _pw, _browser
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _browser = None
    _pw = None


# Matches one posting entry in the DWR JS-literal response, e.g.:
#   s24.id=10513; ... s24.postingDate="11\/09\/2026"; ... s24.title="Sr. Developer";
_POSTING_RE = re.compile(
    r'(?P<var>s\d+)\.id=(?P<id>\d+);'
    r'(?:(?!\.title=).)*?'
    r'\.postingDate="(?P<date>[^"]*)"'
    r'(?:(?!\.title=).)*?'
    r'\.title="(?P<title>[^"]*)"',
    re.DOTALL,
)


def _unescape_js_string(raw: str) -> str:
    """Decode DWR's JS-source-literal escapes (`\\/` and `\\uXXXX`) — the
    response is raw JS source text (read via response.text()), not
    evaluated, so these escapes are never decoded automatically."""
    raw = (raw or "").replace("\\/", "/")
    return re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda m: chr(int(m.group(1), 16)),
        raw,
    )


def _parse_date(raw: str) -> str:
    # "11/09/2026" (DD/MM/YYYY) -> "2026-09-11"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", _unescape_js_string(raw))
    if not m:
        return ""
    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


_cache: list[dict] = []
_cache_filled: bool = False


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    _ensure_browser()

    captured: list[str] = []

    def on_res(res):
        if ".dwr" in res.url:
            try:
                captured.append(res.text())
            except Exception:
                pass

    try:
        page = _browser.new_page(user_agent=_UA)
        page.on("response", on_res)
        page.goto(_LISTING_URL, wait_until="networkidle", timeout=max(timeout, 45) * 1000)
        page.wait_for_timeout(3000)
        # Page size is fixed at 10/page on this tenant (confirmed live:
        # ~27 total postings across 3 pages) — click through every page,
        # capturing each page's own DWR response, until "Next Page" is
        # gone/disabled.
        for _ in range(20):  # generous cap; real tenant has ~3 pages
            try:
                next_btn = page.locator('[aria-label="Next Page"]')
                if next_btn.count() == 0 or not next_btn.first.is_enabled():
                    break
                next_btn.first.click(timeout=5000)
                page.wait_for_timeout(2500)
            except PWTimeoutError:
                break
            except Exception:
                break
        page.close()
    except Exception as exc:
        raise RateLimitError(f"Volkswagen page load failed: {exc}") from exc

    if not captured:
        raise RateLimitError("Volkswagen: DWR job search response never captured")

    collected: list[dict] = []
    seen_ids: set[str] = set()
    for body in captured:
        for m in _POSTING_RE.finditer(body):
            job_id = m.group("id")
            title = _unescape_js_string(m.group("title")).strip()
            if not (job_id and title) or job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            posting_date = _parse_date(m.group("date"))
            collected.append({
                "id": job_id,
                "title": title,
                # No location field exists anywhere in this data source
                # (search response or detail page) — this specific career
                # site is India-scoped (see module docstring), so every job
                # is a bare "India" string. Known accepted precision gap,
                # same as deltatre_fetcher.py.
                "location": "India",
                "posting_date": posting_date,
                "application_url": _DETAIL_URL.format(job_id=job_id),
            })

    _cache[:] = collected
    print(f"[Volkswagen] Cache filled: {len(collected)} jobs company-wide")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Volkswagen (VW Group Digital Solutions India)
    postings from the cached pool.

    keyword/location are accepted for interface compatibility but ignored —
    the whole tenant is tiny (~27 jobs at investigation time) and is cached
    once via a single Playwright page load of the DWR search response.
    """
    _fill_cache(timeout=timeout)
    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) via a plain server-rendered HTML
    GET — no Playwright/session needed for job detail pages on this tenant.
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(2):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("Volkswagen description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                continue
            raise RateLimitError(f"Volkswagen description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Volkswagen description fetch: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    marker = "Return to List"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx + len(marker):]
    description = " ".join(text.split())
    return description, ""
