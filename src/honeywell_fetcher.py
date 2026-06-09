"""Fetches Honeywell job listings from careers.honeywell.com (Oracle HCM CE).

Honeywell's careers portal is a JavaScript SPA — plain requests return empty
API bodies. This fetcher uses a headless Firefox browser (Playwright) so the
page's own KnockoutJS can render job cards; we then extract data from the DOM.

Browser is launched once per process and reused across all fetch calls.
All India jobs are fetched in a single browser session and cached; subsequent
fetch_jobs() calls return slices from that cache.
"""

from __future__ import annotations

import atexit
import re
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_SEARCH_URL = "https://careers.honeywell.com/en/sites/Honeywell/jobs"
_INDIA_LOCATION_ID = "300000000469485"
_PAGE_SIZE = 20
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox only (Chromium blocked by Honeywell/Akamai)
# ---------------------------------------------------------------------------

_pw = None
_browser = None


def _ensure_browser() -> None:
    global _pw, _browser
    if not _PLAYWRIGHT_AVAILABLE:
        raise RateLimitError(
            "playwright not installed — run: pip install playwright && "
            "playwright install firefox"
        )
    if _browser is None:
        _pw = sync_playwright().start()
        _browser = _pw.firefox.launch(headless=True)
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


def _scrape_all_india_jobs() -> list[dict]:
    """Open a Firefox browser, load the Honeywell India jobs page, return all listings."""
    _ensure_browser()

    url = (
        f"{_SEARCH_URL}"
        f"?location=India&locationId={_INDIA_LOCATION_ID}"
        f"&locationLevel=country&mode=location"
    )

    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    try:
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except PWTimeoutError:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except PWTimeoutError:
                pass
            page.wait_for_timeout(5000)

        # Wait for KnockoutJS to render at least one job title
        try:
            page.wait_for_selector("span.job-tile__title", timeout=20000)
        except PWTimeoutError:
            pass

        # Scroll / load-more pagination loop
        prev_count = 0
        for _ in range(30):  # safety cap; each iteration loads ~25 more jobs
            current_count = page.evaluate(
                "document.querySelectorAll('a.job-list-item__link').length"
            )
            if current_count == prev_count:
                break
            prev_count = current_count

            # Try Oracle CE "load more" button first
            btn = page.query_selector(
                "[data-ph-at-id='load-more-jobs'], "
                "button.load-more-jobs, "
                "button:has-text('Load more'), "
                "button:has-text('Show more'), "
                "a:has-text('Load more')"
            )
            if btn and btn.is_visible():
                try:
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    page.wait_for_timeout(3000)
                except Exception:
                    break
            else:
                # Try infinite scroll
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(3000)
                new_count = page.evaluate(
                    "document.querySelectorAll('a.job-list-item__link').length"
                )
                if new_count == current_count:
                    break

        jobs = _scrape_dom(page)
    finally:
        page.close()
        context.close()

    return jobs


def _scrape_dom(page) -> list[dict]:
    """Extract job data from the KnockoutJS-rendered DOM via JS evaluate."""
    raw_jobs = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a.job-list-item__link');
            const jobs = [];
            for (const a of links) {
                const href = a.href || '';
                const idMatch = href.match(/\\/job\\/(\\w+)\\//);
                if (!idMatch) continue;
                const jobId = idMatch[1];

                const ariaId = a.getAttribute('aria-labelledby');
                const card = ariaId ? document.getElementById(ariaId) : null;
                if (!card) continue;

                const titleEl = card.querySelector('.job-tile__title');
                const title = titleEl ? titleEl.textContent.trim() : '';
                if (!title) continue;

                const locEl = card.querySelector('[data-bind*="primaryLocation"]');
                const location = locEl ? locEl.textContent.trim() : 'India';

                // Extract posting date from the date info item
                let date = '';
                const items = card.querySelectorAll('.job-list-item__job-info-item');
                for (const item of items) {
                    const lbl = item.querySelector(
                        '.job-list-item__job-info-label--posting-date'
                    );
                    if (lbl) {
                        const val = item.querySelector('.job-list-item__job-info-value');
                        if (val) date = val.textContent.trim();
                        break;
                    }
                }

                jobs.push({ id: jobId, title, location, date, href });
            }
            return jobs;
        }
    """)

    result: list[dict] = []
    seen: set[str] = set()
    for j in raw_jobs:
        job_id = j.get("id", "")
        title = j.get("title", "")
        if not job_id or not title or job_id in seen:
            continue
        seen.add(job_id)

        href = j.get("href", "")
        app_url = href if href.startswith("http") else f"https://careers.honeywell.com{href}"

        result.append({
            "id": job_id,
            "title": title,
            "location": j.get("location") or "India",
            "posting_date": _parse_posted_date(j.get("date", "")),
            "application_url": app_url,
        })

    return result


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
    """Return a cached slice of Honeywell India jobs.

    The full India job list is scraped once on the first call and held in
    memory.  Slicing by start/num lets matcher.py's pagination loop work
    normally; its deduplication handles the same pool across keyword calls.
    """
    global _jobs_cache, _cache_filled
    if not _cache_filled:
        _cache_filled = True  # Set early to prevent retry storm if scrape fails
        try:
            _jobs_cache = _scrape_all_india_jobs()
        except Exception as exc:
            raise RateLimitError(f"Honeywell Playwright scrape failed: {exc}") from exc

    return _jobs_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 30,
) -> tuple[str, str]:
    """Fetch the full description for one Honeywell job via headless Firefox.

    Returns (description_text, posting_date).
    """
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    try:
        try:
            page.goto(application_url, wait_until="networkidle", timeout=timeout * 1000)
        except PWTimeoutError:
            try:
                page.goto(
                    application_url, wait_until="domcontentloaded", timeout=timeout * 1000
                )
            except PWTimeoutError:
                pass
            page.wait_for_timeout(4000)

        # Wait for job description content to render
        try:
            page.wait_for_selector(
                ".job-description, [data-ph-at-id*='description' i], "
                "[class*='job-content' i], article, main",
                timeout=15000,
            )
        except PWTimeoutError:
            pass

        # Prefer specific description containers; avoid tiny "description label" divs
        raw_text = ""
        for sel in (
            "[data-ph-at-id*='description' i]",
            ".job-description",
            "[class*='job-content' i]",
            "article",
            "main",
        ):
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if len(t) > 100:  # ignore short/label elements
                    raw_text = t
                    break

        if not raw_text:
            raw_text = page.inner_text("body")

        text = " ".join(raw_text.split())

        date_m = re.search(
            r"\b(\d{4}-\d{2}-\d{2}|[A-Za-z]+ \d{1,2},? \d{4}|\d{1,2} [A-Za-z]+ \d{4})\b",
            text,
        )
        posting_date = _parse_posted_date(date_m.group(1)) if date_m else ""

        return text, posting_date

    except Exception as exc:
        raise RateLimitError(f"Honeywell description fetch failed: {exc}") from exc
    finally:
        page.close()
        context.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_posted_date(raw: str) -> str:
    """Normalise any date string to YYYY-MM-DD; return '' on failure."""
    if not raw:
        return ""
    s = " ".join(raw.split())
    iso_m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if iso_m:
        return iso_m.group(1)
    for fmt in (
        "%m/%d/%Y",     # 06/09/2026  ← Oracle CE format
        "%B %d, %Y",    # June 9, 2026
        "%B %d %Y",
        "%b %d, %Y",
        "%b %d %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%d/%m/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""
