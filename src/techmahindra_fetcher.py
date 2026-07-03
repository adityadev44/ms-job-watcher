"""Fetches Tech Mahindra job listings via careers.techmahindra.com.

This is a legacy ASP.NET WebForms site with an AJAX UpdatePanel-based search
form — selecting the country dropdown fires a partial postback
(`__doPostBack`) that requires an exact ViewState/EventValidation/
X-MicrosoftAjax header dance to replicate with plain `requests` (verified:
a hand-built POST without the ScriptManager delta headers returns a raw
ASP.NET 500 error page). Rather than hand-parse the AJAX "delta" response
format, this fetcher drives a real headless Firefox (Playwright) through the
country-select + pagination flow and reads the rendered DOM — same approach
as honeywell_fetcher.py.

Pagination uses a repeater pager (`rptPager`) whose control IDs shift per
page; calling `__doPostBack` on the ">>" link directly via `page.evaluate()`
sidesteps a persistent OneTrust cookie-consent overlay that blocks real
Playwright clicks on every pager link.

Job-detail pages (JobDetails.aspx) ARE plain server-rendered HTML — no
browser needed there. The full JD prose sits between the "Job Description"
metadata block and a "Buddy referal policy" boilerplate footer.
"""
from __future__ import annotations

import atexit
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_SEARCH_URL = "https://careers.techmahindra.com/CurrentOpportunity.aspx"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox, reused across the process
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
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    jobs: list[dict] = []
    seen_codes: set[str] = set()

    try:
        page.goto(_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        page.select_option("select[name='ctl00$ContentPlaceHolder1$ddlCountry']", "IND")
        page.wait_for_timeout(4000)

        for _ in range(60):  # safety cap
            raw = page.eval_on_selector_all(
                "a[href*='JobDetails.aspx']",
                """els => els.map(e => ({
                    href: e.href,
                    text: e.closest('div, li, tr')?.innerText || ''
                }))"""
            )
            new_this_page = 0
            for item in raw:
                href = item.get("href", "")
                m = re.search(r"JobCode=([^&]+)", href)
                if not m:
                    continue
                code = m.group(1)
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                new_this_page += 1

                block = item.get("text", "")
                title_m = re.search(r"^(?:IT|BPS)\s*\n(.+)", block)
                title = title_m.group(1).strip() if title_m else block.split("\n")[0].strip()
                loc_m = re.search(r"Location\s*:\s*([A-Za-z ,]+)", block)
                city = loc_m.group(1).strip() if loc_m else ""

                jobs.append({
                    "id": code,
                    "title": title,
                    "location": f"{city}, India" if city else "India",
                    "posting_date": "",  # filled in on description fetch
                    "application_url": href,
                })

            if new_this_page == 0:
                break

            target = page.evaluate(
                """() => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const el = links.find(a => a.textContent.trim() === '>>');
                    if (!el) return null;
                    const m = el.getAttribute('href').match(/__doPostBack\\('([^']+)'/);
                    return m ? m[1] : null;
                }"""
            )
            if not target:
                break
            page.evaluate(f"__doPostBack('{target}', '')")
            page.wait_for_timeout(3000)
    finally:
        page.close()
        context.close()

    return jobs


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a cached slice of Tech Mahindra India jobs.

    The full India job list is scraped once (country dropdown + pagination
    postbacks) and held in memory; keyword is not applied server-side.
    """
    global _jobs_cache, _cache_filled
    if not _cache_filled:
        _cache_filled = True
        try:
            _jobs_cache = _scrape_all_india_jobs()
        except Exception as exc:
            raise RateLimitError(f"Tech Mahindra Playwright scrape failed: {exc}") from exc

    return _jobs_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the full description via plain HTTP (server-rendered detail page)."""
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Tech Mahindra description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Tech Mahindra description fetch: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    start_idx = text.find("Job Description")
    end_idx = text.find("Buddy referal policy")
    if start_idx == -1:
        start_idx = 0
    if end_idx == -1 or end_idx < start_idx:
        end_idx = start_idx + 4000
    description = " ".join(text[start_idx:end_idx].split())

    date_m = re.search(r"Job Post Date\s*:\s*(\d{2}/\d{2}/\d{4})", text)
    posting_date = ""
    if date_m:
        try:
            posting_date = datetime.strptime(date_m.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    return description, posting_date
