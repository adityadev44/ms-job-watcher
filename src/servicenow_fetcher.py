"""Fetches ServiceNow job listings from careers.servicenow.com.

ATS: custom in-house career site on Umbraco CMS (confirmed via the live
`GET /umbraco/jobboard/CandidateJobs/GetRecentJobs` XHR captured from a real
browser session), NOT any third-party ATS vendor -- Greenhouse/Lever/Ashby/
Workday all 404/empty for this company.

**Every path on this domain sits behind a Cloudflare managed challenge that
plain `requests` cannot pass** -- a bare GET of `/jobs/`, `/robots.txt`'s own
referenced paths, and even the JSON `GetRecentJobs` API endpoint all return
"Just a moment..." (HTTP 403) to a scripted client with no User-Agent
history, regardless of headers sent. A real browser (headless Firefox via
Playwright) passes cleanly and gets HTTP 200 on every path tested -- same
class of problem as BNP Paribas/Honeywell/IBM, solved the same way: drive
Firefox throughout, no plain-`requests` fallback exists for this company.

Good news once past Cloudflare: `/jobs/` is genuinely server-rendered HTML
(Umbraco, not a client-side SPA) -- a GET with query params IS honored
server-side:

    GET https://careers.servicenow.com/jobs/?country=India&page={n}#results

returns real job cards embedded directly in the HTML (`<div class="card
card-job">`), no separate XHR/JSON call needed once the page has loaded in a
real browser. Verified live 2026-08-31: 39 of ~900+ global postings are
India (`Displaying 1 to 20 of 39 matching jobs`), paginated 20/page via
`?page=2`, `?page=3`, etc.

Job cards give id (from `data-id` on the `.js-job` action div, matching the
URL's leading path segment), title, and a bare city name (e.g. "Hyderabad")
with no "India" substring -- ", India" is appended since the country facet
already guarantees it. No posting date on the list card; left blank until
the detail fetch fills it in (BNP Paribas pattern).

Job-detail pages (`/jobs/{id}/{slug}/`) carry a clean schema.org JobPosting
JSON-LD block at `<script id="js-job-posting" type="application/ld+json">`
(note the `id` attribute -- there are OTHER `application/ld+json` blocks on
the same page for breadcrumbs/organization schema, so anchoring on this
specific `id` avoids picking up the wrong one, a variant of the Wipro/
HCLTech "itemprop=description appears twice" lesson). `datePosted` is
already ISO `YYYY-MM-DD`, no parsing needed.

Titles/descriptions carry strong, direct signal -- ServiceNow is a genuine
AI-workflow-platform employer with a large Hyderabad engineering centre, not
an IT-services shop with generic bands. Live sample: "Sr Software Engineer"
(Hyderabad) explicitly describes "AI-driven capabilities and agents", LLMs,
retrieval, and agent frameworks; other India titles include "Senior ML
Research Engineer/Scientist" and "Staff Technical Product Manager - AI/LLM
expertise + AI Evaluation Science". require_tech_in_description is NOT
enabled -- titles are specific engineering/research titles, not generic
level-banded IT-services titles.
"""
from __future__ import annotations

import atexit
import json
import re
import time

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_ORIGIN = "https://careers.servicenow.com"
_LIST_URL = f"{_ORIGIN}/jobs/"
_MAX_PAGES = 15  # safety cap; observed pool needs 2 (39 jobs / 20 per page)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"


class RateLimitError(Exception):
    """Raised when the site is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton -- Firefox required, Cloudflare blocks plain requests
# ---------------------------------------------------------------------------

_pw = None
_browser = None


def _ensure_browser() -> None:
    global _pw, _browser
    if not _PLAYWRIGHT_AVAILABLE:
        raise RateLimitError(
            "playwright not installed -- run: pip install playwright && "
            "playwright install firefox"
        )
    if _browser is None:
        with STARTUP_LOCK:
            _pw = sync_playwright().start()
            try:
                _browser = _pw.firefox.launch(headless=True)
            except Exception:
                # A failed launch (e.g. a stale/wrong-build cached browser)
                # must not leave `_pw` pointing at a live, never-stopped
                # Playwright instance -- a caller-level retry would then
                # call sync_playwright().start() again on the SAME thread
                # while the leaked instance's own dispatcher loop is still
                # alive, which fails with the unrelated-looking "Sync API
                # inside the asyncio loop" error and masks the real cause.
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
# Job-list cache -- scrape once per process, serve slices on every fetch_jobs()
# ---------------------------------------------------------------------------

_jobs_cache: list[dict] = []
_cache_filled: bool = False


def _parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("div.card.card-job"):
        link = card.select_one("a.js-view-job")
        actions = card.select_one("div.js-job")
        if not link or not actions:
            continue
        href = link.get("href", "").strip()
        title = link.get_text(strip=True)
        job_id = (actions.get("data-id") or "").strip()
        if not (href and title and job_id):
            continue
        loc_li = card.select_one("ul.job-meta li")
        city = loc_li.get_text(strip=True) if loc_li else ""
        loc = city if "india" in city.lower() else (f"{city}, India" if city else "India")
        out.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": "",  # filled in on description fetch
            "application_url": f"{_ORIGIN}{href}" if href.startswith("/") else href,
        })
    return out


def _scrape_all_india_jobs(timeout: int = 30) -> list[dict]:
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    collected: list[dict] = []
    seen_ids: set[str] = set()
    try:
        pg = 1
        while pg <= _MAX_PAGES:
            url = f"{_LIST_URL}?country=India&page={pg}#results"
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            except PWTimeoutError:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(3000)

            html = page.content()
            cards = _parse_cards(html)
            new_any = False
            for c in cards:
                if c["id"] in seen_ids:
                    continue
                seen_ids.add(c["id"])
                collected.append(c)
                new_any = True
            if not cards or not new_any:
                break
            pg += 1
    finally:
        page.close()
        context.close()

    return collected


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a cached slice of ServiceNow India jobs.

    keyword is ignored (the shared matcher does real title/skill filtering);
    location's India scoping is applied server-side via `?country=India`.
    Full India pool scraped once and cached, then served in slices.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before attempting -- avoid a retry storm
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                _jobs_cache[:] = _scrape_all_india_jobs(timeout=timeout)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            raise RateLimitError(f"ServiceNow cache fill failed: {last_exc}")
        print(f"[ServiceNow] Cache filled: {len(_jobs_cache)} India jobs")

    return _jobs_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) parsed from the detail page's
    schema.org JobPosting JSON-LD block (anchored on id="js-job-posting").
    """
    _ensure_browser()

    last_exc: Exception | None = None
    for attempt in range(3):
        context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
        page = context.new_page()
        try:
            try:
                page.goto(application_url, wait_until="networkidle", timeout=timeout * 1000)
            except PWTimeoutError:
                page.goto(application_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(3000)

            html = page.content()
            m = re.search(
                r'<script id="js-job-posting" type="application/ld\+json">(.*?)</script>',
                html,
                re.DOTALL,
            )
            if not m:
                return "", ""

            data = json.loads(m.group(1))
            desc_html = data.get("description", "") or ""
            text = BeautifulSoup(desc_html, "html.parser").get_text(separator=" ", strip=True)
            posted = (data.get("datePosted") or "").strip()
            return text, posted
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"ServiceNow description fetch failed: {exc}"
            ) from exc
        finally:
            page.close()
            context.close()

    raise RateLimitError(f"ServiceNow description fetch failed: {last_exc}")
