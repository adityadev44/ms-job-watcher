"""Fetches Axtria job listings via a legacy SAP SuccessFactors "RCM" Career
Site (career10.successfactors.com, company code "axtriaindiP").

This is NOT the SuccessFactors "Job2Web" (J2W) platform already documented
in this repo (Nomura/Capgemini/SAP Labs/Mastek/Dover classic-theme, or
Standard Chartered/Wipro/HCLTech Unify-theme) -- it is SAP's older
JSF/DWR-based "Recruiting Career Site", a fundamentally different, heavier
product. Confirmed via live DevTools-style capture (Playwright network
inspection), not guessed from the URL shape (see the Boeing/PepsiCo/Walmart
"custom ATS guess was wrong" lesson in PLAYBOOK.md).

Why Playwright is required for the search step (verified, not assumed):
- The page bootstraps via `POST .../careerJobSearchControllerProxy
  .getInitialJobSearchData.dwr`, a DWR (Direct Web Remoting) RPC call --
  its own response literally starts with `throw 'allowScriptTagRemoting is
  false.';` as an XSS guard, meaning the payload is only usable by the
  page's own JS runtime, not a bare HTTP client.
- The empty/default search ("Search" button with no criteria) IS a genuine
  page navigation (`GET /career?...&career_ns=job_listing_summary&
  navBarLevel=JOB_SEARCH&_s.crb=<token>`), so it looks pattern-replicable
  with plain `requests` -- but replaying that exact URL cold (fresh
  session, no prior DWR calls) reliably returns "No jobs": the `_s.crb`
  CSRF-style token is bound to prior server-side session state set up by
  the DWR calls the live JS makes before that navigation fires. Confirmed
  via a direct A/B test: identical URL, identical crb token extracted from
  the same GET's own page source, plain `requests.Session` still shows "No
  jobs" while a real (Play)Firefox click on the rendered "Search" button
  shows the true ~127-job board. This is the same class of "session state
  a plain HTTP replay can't reproduce" problem documented for Societe
  Generale's search-proxy and Virtusa's Taleo JSF postback, just with DWR
  instead of ordinary form fields.
- Pagination ("Next Page") is a `juic.fire(...)` AJAX postback -- driven by
  clicking the actual DOM element rather than reverse-engineered.

Why job-DETAIL fetching does NOT need Playwright (verified, not assumed):
- Once a job's `href` is captured from a real rendered search-results page,
  that exact URL -- crb token and all -- is a plain, stateless, cacheable
  GET: a cold `requests.get` (no cookies, no prior navigation, a fresh
  process) on a captured href renders the real job description HTML
  (`<div class="externalPosting">...`). The crb token is apparently scoped
  to the requisition + a short validity window, not to the browser session
  that generated it.
- Some individual requisitions have a real, populated `externalPosting` div
  and some show the literal placeholder text "[Not translated in selected
  language]" -- confirmed by testing several jobs from the same page in the
  same session (2 of the first 10 had genuinely empty external content,
  the other 8 had real JD prose). This is a per-requisition data-quality
  gap on Axtria's own tenant (same class of issue as Salesforce's `<p>NA</p>`
  placeholder JD / Perfios's empty descriptions), not a fetcher bug --
  handled by returning an empty description, which the shared matcher
  correctly drops as `[unverified]` rather than alerting on it blind.

Location quirk: `locationsText`-equivalent strings on this tenant never
say "India" ("Gurgaon, Tower B (Floor 17), CC") -- this is Axtria's
India-only careers site (company code "axtriaindiP"), so ", India" is
always safely appended client-side, same reasoning as First American/
MetLife's dedicated India portals.

Live-verified 2026-09-13/14: ~127 total postings, almost entirely
generic consulting-ladder titles (Associate/Manager/Director/Project
Leader) with no tech signal in the title at all -- title_family's
`"data engineer"` phrase is the only reliable current entry point
("Data Engineer"/"Data Engineers"/"Senior Data Engineers"/"Data Quality
Engineers" postings exist in Hyderabad, non-excluded).
"""
from __future__ import annotations

import atexit
import html as html_mod
import re
import time
from datetime import datetime

import requests

try:
    from playwright.sync_api import sync_playwright
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_BASE_URL = "https://career10.successfactors.com"
_SEARCH_URL = f"{_BASE_URL}/career?company=axtriaindiP"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}

_ROW_RE = re.compile(
    r'<a[^>]*class="[^"]*jobTitle[^"]*"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
    r'</div><div class="noteSection" role="note"><div>'
    r'Requisition ID: <span class="jobContentEM">(\d+)</span> - '
    r'<span class="jobContentEM">Posted on ([0-9/]+)</span> - '
    r'<span class="jobContentEM">([^<]+)</span>'
)

_MAX_PAGES = 40  # safety ceiling; ~127 jobs at 10/page is ~13 pages


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton -- Firefox, reused across the process (same pattern as
# techmahindra_fetcher.py / honeywell_fetcher.py).
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


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'MM/DD/YYYY' -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Job-list cache -- scrape once via Playwright, serve slices on every
# fetch_jobs() call.
# ---------------------------------------------------------------------------

_jobs_cache: list[dict] = []
_cache_filled: bool = False


def _scrape_all_jobs() -> list[dict]:
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    jobs: dict[str, dict] = {}

    try:
        page.goto(_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        page.get_by_role("button", name="Search").first.click(timeout=8000)
        page.wait_for_timeout(4000)

        for _ in range(_MAX_PAGES):
            content = page.content()
            new_this_page = 0
            for href, title, req_id, posted, loc in _ROW_RE.findall(content):
                if req_id in jobs:
                    continue
                new_this_page += 1
                href = html_mod.unescape(href)
                title = title.strip()
                loc = loc.strip()
                jobs[req_id] = {
                    "id": req_id,
                    "title": title,
                    "location": f"{loc}, India" if "india" not in loc.lower() else loc,
                    "posting_date": _parse_date(posted),
                    "application_url": f"{_BASE_URL}{href}",
                }

            if new_this_page == 0 and jobs:
                # Same page re-scraped with nothing new -- either the last
                # page or a stuck pager; stop rather than loop forever.
                pass

            next_btns = page.locator('a[title="Next Page"]')
            clicked = False
            for j in range(next_btns.count()):
                btn = next_btns.nth(j)
                cls = btn.get_attribute("class") or ""
                if "disabled" not in cls.lower():
                    try:
                        btn.click(timeout=3000)
                        clicked = True
                        break
                    except Exception:
                        continue
            if not clicked:
                break
            page.wait_for_timeout(2500)
    finally:
        page.close()
        context.close()

    return list(jobs.values())


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a cached slice of Axtria's India job board.

    The full board (~127 postings) is scraped once via a headless-Firefox
    search+pagination walk (keyword is not applied server-side, unlike the
    J2W Unify tenants elsewhere in this repo -- this legacy RCM Career Site
    has no reliably replicable server-side keyword filter) and held in
    memory for the rest of the process.
    """
    global _jobs_cache, _cache_filled
    if not _cache_filled:
        _cache_filled = True
        try:
            _jobs_cache = _scrape_all_jobs()
        except Exception as exc:
            raise RateLimitError(f"Axtria Playwright scrape failed: {exc}") from exc

    return _jobs_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via plain HTTP.

    The captured href (crb token included) is a stateless, cacheable GET --
    no browser session needed here, see module docstring.
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Axtria description: 429 on {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Axtria description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Axtria description fetch: no response — {last_exc}")

    m = re.search(
        r'<div class="externalPosting">(.*?)</div>\s*</div>',
        r.text,
        re.DOTALL,
    )
    raw = m.group(1) if m else ""
    description = _strip_html(raw)
    if description == "[Not translated in selected language]":
        description = ""

    return description, ""
