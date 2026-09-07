"""Fetches GlobalLogic India job listings via their WordPress career site.

GlobalLogic's career search page (globallogic.com/in/career-search-page/)
was plain-requests accessible until September 2026, when Incapsula bot
protection was enabled site-wide. Headless Firefox via Playwright passes
cleanly (confirmed locally 2026-09-07); plain requests return HTTP 403.

Key facts re-confirmed 2026-09-07 via Playwright:
  - Search URL: https://www.globallogic.com/in/career-search-page/?location=india
    Page 2+: .../in/career-search-page/page/{N}/?location=india
  - ~143 India jobs across ~15 pages of 10 per page (WP theme, fixed size).
  - Job cards are SSR'd in <a class="job_box"> inside
    <div class="career_filter_result">.
  - Title: <h4> text — includes trailing IRC code (e.g. "Boomi Developer
    IRC304007"); strip the "IRC\\d+" suffix before storing.
  - Location: <span class="job_location"> spans; the bare "India" span is
    skipped; remaining spans are city names.
  - Job ID: IRC code extracted from the detail URL slug (e.g. "irc304007").
  - Application URL: href attribute on the job_box <a> (absolute,
    e.g. https://www.globallogic.com/in/careers/boomi-developer-irc304007/).
  - Posting date: NOT in listing; detail page div.career_banner_sub_head
    contains "Published on D Month YYYY".
  - Description: div.career_detail_area on the individual job page.

Keywords param on the search page is applied client-side by WP JS, not
server-side — fetching without a keyword param returns all India jobs.
Full board is cached once per process; matcher.py does title/skill filtering.

Incapsula injects a tracking script on every page (not a challenge) — the
real HTML including job listings loads normally for headless Firefox.
"""
from __future__ import annotations

import atexit
import html as html_mod
import re
import time
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_BASE_URL = "https://www.globallogic.com"
_SEARCH_URL = f"{_BASE_URL}/in/career-search-page/"
_PAGE_SIZE = 10  # WP theme fixed size

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0"

_IRC_RE = re.compile(r"-(irc\d+)/?$", re.I)


class RateLimitError(Exception):
    """Raised on persistent navigation failure from GlobalLogic."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox required; Incapsula blocks plain requests
# ---------------------------------------------------------------------------

_pw = None
_browser = None
_live_context = None


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


def _ensure_live_context(timeout: int = 30) -> None:
    """Create (or re-create) the module-level Incapsula-cleared context.

    Navigates to the search page once so Incapsula's JS challenge runs and
    sets its tracking cookie.  The context is kept alive after first use.
    """
    global _live_context
    _ensure_browser()
    if _live_context is not None:
        return

    ctx = _browser.new_context(
        user_agent=_UA,
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    try:
        try:
            page.goto(_SEARCH_URL, wait_until="networkidle", timeout=timeout * 1000)
        except PWTimeoutError:
            page.goto(_SEARCH_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(4000)
    finally:
        page.close()

    _live_context = ctx


def _get_page_html(url: str, timeout: int = 30) -> str:
    """Navigate to url and return page.content().  Re-creates context on failure."""
    global _live_context
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_live_context(timeout=30)
            page = _live_context.new_page()
            try:
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                except PWTimeoutError:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                    page.wait_for_timeout(3000)
                return page.content()
            finally:
                page.close()
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
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"GlobalLogic navigation failed after 3 attempts: {exc}"
            ) from exc
    raise RateLimitError(f"GlobalLogic: no response — {last_exc}")


# ---------------------------------------------------------------------------
# HTML parsers
# ---------------------------------------------------------------------------

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False


def _parse_listing_page(html: str) -> list[dict]:
    """Extract job dicts from one career_filter_result page."""
    if not _BS4_AVAILABLE:
        raise RateLimitError("beautifulsoup4 not installed")

    from bs4 import BeautifulSoup  # noqa: PLC0415
    soup = BeautifulSoup(html, "html.parser")
    result_area = soup.find(class_="career_filter_result")
    if not result_area:
        return []

    boxes = result_area.find_all("a", class_="job_box")
    jobs = []
    for box in boxes:
        href = (box.get("href") or "").strip().rstrip("/")
        if not href:
            continue

        irc_m = _IRC_RE.search(href)
        job_id = irc_m.group(1).lower() if irc_m else ""
        if not job_id:
            continue

        h4 = box.find("h4")
        raw_title = h4.get_text(strip=True) if h4 else ""
        title = re.sub(r"\s+IRC\d+$", "", raw_title, flags=re.I).strip()
        if not title:
            continue

        spans = box.find_all("span", class_="job_location")
        cities = [s.get_text(strip=True) for s in spans
                  if s.get_text(strip=True).lower() != "india"]
        location = f"{', '.join(cities)}, India" if cities else "India"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",
            "application_url": href if href.startswith("http") else f"{_BASE_URL}{href}",
        })

    return jobs


def _parse_detail_date(raw: str) -> str:
    """'Published on 4 September 2026' → '2026-09-04'."""
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw or "")
    if not m:
        return ""
    try:
        return datetime.strptime(
            f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Job-list cache — paginate full India board once, serve slices
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_cache_filled: bool = False
_FIRST_PAGE_IDS: set[str] | None = None
_MAX_PAGES = 30  # safety cap (~300 jobs)


def _fill_cache(timeout: int = 30) -> None:
    global _cache_filled, _job_cache, _FIRST_PAGE_IDS
    if _cache_filled:
        return
    _cache_filled = True

    _ensure_live_context(timeout=timeout)

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None

    for page_num in range(1, _MAX_PAGES + 1):
        if page_num == 1:
            url = f"{_SEARCH_URL}?location=india"
        else:
            url = f"{_SEARCH_URL}page/{page_num}/?location=india"

        html = _get_page_html(url, timeout=timeout)
        jobs = _parse_listing_page(html)
        if not jobs:
            break

        page_ids = {j["id"] for j in jobs}

        if page_num == 1:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break  # wraparound — ATS replayed page 1

        new_this_page = 0
        for j in jobs:
            if j["id"] in seen_ids:
                continue
            seen_ids.add(j["id"])
            collected.append(j)
            new_this_page += 1

        if new_this_page == 0:
            break
        if len(jobs) < _PAGE_SIZE:
            break  # partial page → last page

        if page_num < _MAX_PAGES:
            time.sleep(0.3)  # polite between pages

    _job_cache = collected
    print(f"[GlobalLogic] Cache filled: {len(collected)} total India jobs")


# ---------------------------------------------------------------------------
# Public API
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
    """Return a slice of GlobalLogic India job listings.

    keyword and location are ignored server-side — the India-scoped search
    URL returns the full India board regardless.  Full board is cached once
    per process; matcher.py does title/skill filtering.
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
                    f"GlobalLogic cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start: start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for one GlobalLogic job.

    Navigates to the individual job page via the Incapsula-cleared context.
    Detail page: div.career_detail_area (description),
                 div.career_banner_sub_head (date string).
    """
    if not _BS4_AVAILABLE:
        raise RateLimitError("beautifulsoup4 not installed")

    from bs4 import BeautifulSoup  # noqa: PLC0415

    html = _get_page_html(application_url, timeout=timeout)
    soup = BeautifulSoup(html, "html.parser")

    desc_div = soup.find(class_="career_detail_area")
    description = ""
    if desc_div:
        raw = html_mod.unescape(desc_div.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    banner_sub = soup.find(class_="career_banner_sub_head")
    if banner_sub:
        posting_date = _parse_detail_date(banner_sub.get_text(strip=True))

    return description, posting_date
