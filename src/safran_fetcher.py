"""Fetches Safran India job listings from safran-group.com (in-house Drupal board).

ATS identification (Step 1, verified live 2026-09-13): careers.safran-group.com
is NOT a third-party ATS vendor (no Workday/SuccessFactors/iCIMS/Greenhouse
signal anywhere) -- `www.safran-group.com/jobs` is a first-party Drupal site
(robots.txt shows classic Drupal paths like `/node/add/`, `/user/register`)
with its own custom job-search View. The domain is Cloudflare-fronted with a
JS challenge that returns a clean HTTP 403 to plain `requests`/`WebFetch`
(same shape as Dover's `dovercareers.com`) -- confirmed both curl and
WebFetch get 403 with zero body, while plain headless Chromium (no special
anti-detection flags needed, unlike NatWest/IBM) passes through fine and
renders the full page. Playwright is therefore required end-to-end, same
class of company as Honeywell/Tech Mahindra/Virtusa in this repo, just for a
different reason (Cloudflare challenge, not a JS-only SPA backend).

Country filtering uses a taxonomy-style facet value, not the literal string
"India" -- found by inspecting the rendered `<select id="edit-countries">`
options: `countries=1083-india`. `search=<keyword>` genuinely narrows
server-side (verified live: unfiltered India pool 192 jobs -> 48 for
"software"), but rather than repeat ~10 keyword searches x ~16 pages each
through a slow Cloudflare-gated browser flow, this fetcher caches the full
unfiltered India pool once (paginating `?countries=1083-india&page=N`, 12
jobs/page, 0-indexed, confirmed via the page's own "1 Page 2 Page 3 ... 16"
control) and lets the shared matcher.py handle keyword/title/skill
relevance afterward -- same "cache once, let matcher filter" pattern as
Boeing/UBS/Deutsche Bank. Register this slug in `_IGNORES_KEYWORDS`.

Verified live 2026-09-13: India pool is a genuine, sizeable ~192 jobs across
Bengaluru/Hyderabad/New Delhi (Safran Aircraft Engines, Safran Electrical &
Power, Safran Engineering Services, Safran Electronics & Defense entities),
including real software engineering titles seen directly in a live sample:
"Software V&V Engineer M-F for Bangalore", "Senior Engineer - Lead Engineer -
Software Platform M-F for Bangalore", "XATIS: Web Full-Stack Software
Developer" -- a genuine current software-engineering hiring signal, not just
mechanical/manufacturing roles.

Each job-detail page carries a clean schema.org JobPosting JSON-LD block
(same pattern as Boeing/Optum/SAP Labs) -- `fetch_job_description` reads
that directly from the Chromium-rendered page instead of guessing at DOM
selectors. `datePosted` is already ISO (YYYY-MM-DD).

List-page date format is "MM.DD.YYYY" (e.g. "09.11.2026" = Sept 11, 2026,
cross-checked against the same job's JSON-LD `datePosted: "2026-09-11"") --
`_parse_list_date` converts it; the JSON-LD date is preferred when available
and is what `fetch_job_description` returns.
"""

from __future__ import annotations

import atexit
import re

try:
    import json as _json

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_BASE_URL = "https://www.safran-group.com"
_SEARCH_URL = f"{_BASE_URL}/jobs"
_INDIA_COUNTRY_PARAM = "1083-india"
_PAGE_SIZE = 12  # fixed by this Drupal view; not configurable via query param
_MAX_PAGES = 30  # safety cap (192 jobs / 12 per page = 16 pages today)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class RateLimitError(Exception):
    """Raised when the site is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton -- Chromium (no anti-detection flags needed; confirmed
# a plain headless launch passes Safran's Cloudflare challenge)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Job-list cache — scrape once, serve slices on every fetch_jobs() call
# ---------------------------------------------------------------------------

_jobs_cache: list[dict] = []
_cache_filled: bool = False


def _parse_list_date(raw: str) -> str:
    """Convert the list page's 'MM.DD.YYYY' date to 'YYYY-MM-DD'."""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", raw.strip())
    if not m:
        return ""
    month, day, year = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _scrape_page(page, page_num: int) -> list[dict]:
    url = f"{_SEARCH_URL}?countries={_INDIA_COUNTRY_PARAM}&page={page_num}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PWTimeoutError:
        pass
    try:
        page.wait_for_selector(".c-offer-item, .c-list-job-offers", timeout=20000)
    except PWTimeoutError:
        pass
    page.wait_for_timeout(1500)

    raw_jobs = page.evaluate(
        """
        () => {
            const items = document.querySelectorAll('.c-offer-item');
            const out = [];
            for (const item of items) {
                const a = item.querySelector('a.c-offer-item__title');
                if (!a) continue;
                const title = a.textContent.trim();
                const href = a.href || '';
                const dateEl = item.querySelector('.c-offer-item__date');
                const date = dateEl ? dateEl.textContent.trim() : '';

                let location = '';
                const infos = item.querySelectorAll('.c-offer-item__infos__item');
                for (const info of infos) {
                    const icon = info.querySelector('.c-offer-item__infos__item__icon');
                    if (icon && icon.classList.contains('icon-location') &&
                        !icon.classList.contains('icon-hierarchy')) {
                        location = info.textContent.trim();
                        break;
                    }
                }
                out.push({ title, href, date, location });
            }
            return out;
        }
        """
    )

    jobs: list[dict] = []
    for j in raw_jobs:
        href = j.get("href", "")
        title = j.get("title", "")
        if not href or not title:
            continue
        id_match = re.search(r"-(\d+)$", href)
        job_id = id_match.group(1) if id_match else href
        jobs.append({
            "id": job_id,
            "title": title,
            "location": j.get("location") or "India",
            "posting_date": _parse_list_date(j.get("date", "")),
            "application_url": href,
        })

    return jobs


def _scrape_all_india_jobs() -> list[dict]:
    """Paginate the full India pool, one fresh browser context per page.

    Reusing a single Playwright page/context across sequential same-origin
    navigations tripped Cloudflare's WAF after exactly one extra request
    (a clean "Sorry, you have been blocked" page even with several seconds'
    delay between navigations) — confirmed live 2026-09-13. A brand-new
    context per page request (i.e. a fresh cookie/challenge state each
    time) reliably avoids the block; each page load costs ~3s, so a full
    16-page India scrape is ~50s, a one-time per-process cost.
    """
    _ensure_browser()

    all_jobs: list[dict] = []
    seen_ids: set[str] = set()
    for page_num in range(_MAX_PAGES):
        context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
        page = context.new_page()
        try:
            batch = _scrape_page(page, page_num)
        finally:
            page.close()
            context.close()

        if not batch:
            break
        new_count = 0
        for j in batch:
            if j["id"] not in seen_ids:
                seen_ids.add(j["id"])
                all_jobs.append(j)
                new_count += 1
        if new_count == 0:
            # Same jobs as the previous page — pagination has wrapped or ended.
            break

    return all_jobs


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
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
    """Return a cached slice of Safran India jobs.

    The full India job list is scraped once (paginating the Drupal view)
    and held in memory; slicing by start/num lets matcher.py's pagination
    loop terminate naturally. Server-side `search=` narrowing exists but is
    intentionally not used here (see module docstring) — keyword/title/skill
    relevance is left entirely to the shared matcher.
    """
    global _jobs_cache, _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before scraping to avoid a retry storm
        try:
            _jobs_cache = _scrape_all_india_jobs()
        except Exception as exc:
            raise RateLimitError(f"Safran Playwright scrape failed: {exc}") from exc

    return _jobs_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 30,
) -> tuple[str, str]:
    """Fetch a Safran job's full description + posting date via headless Chromium.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block
    (same pattern as Boeing/Optum) — read directly from the rendered page.
    """
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    try:
        try:
            page.goto(application_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except PWTimeoutError:
            pass
        page.wait_for_timeout(2000)

        scripts = page.query_selector_all('script[type="application/ld+json"]')
        for script in scripts:
            raw = script.text_content() or ""
            try:
                data = _json.loads(raw)
            except _json.JSONDecodeError:
                continue

            candidates = data.get("@graph", [data]) if isinstance(data, dict) else []
            for entry in candidates:
                if not isinstance(entry, dict) or entry.get("@type") != "JobPosting":
                    continue
                description = " ".join((entry.get("description") or "").split())
                posting_date = (entry.get("datePosted") or "").strip()
                if description:
                    return description, posting_date

        # Fallback: plain visible text of the description area if JSON-LD is missing.
        el = page.query_selector("main") or page.query_selector("body")
        text = " ".join((el.inner_text() if el else "").split())
        return text, ""
    except Exception as exc:
        raise RateLimitError(f"Safran description fetch failed: {exc}") from exc
    finally:
        page.close()
        context.close()
