"""Fetches Toyota job listings — Toyota Connected India Pvt Ltd, via the
Darwinbox candidatev2 candidate portal (Playwright/Firefox required, see
below).

ATS discovery (2026-09-13/14): investigated two separate Toyota India
entities before choosing which one to onboard:

- **Toyota Kirloskar Motor** (`careers.toyotabharat.com`), the manufacturing
  plant near Bengaluru (Bidadi), runs SAP SuccessFactors "Job2Web Unify"
  (same family as ZF/Standard Chartered/Wipro elsewhere in this repo) — the
  real AJAX shape was captured live (`POST /services/recruiting/v1/jobs`
  with `{"location": "India", "keywords": "...", "categoryId": 0, ...}`,
  a slightly different body shape than the other Unify tenants in this
  repo, discovered via a real Playwright network capture after the classic
  header/CSRF-token replay attempts that work at other Unify tenants kept
  302-redirecting to an error page here). Live-queried: this ENTIRE
  company-wide tenant (not just India-filtered — an empty `location` filter
  returns the same count) currently has exactly **2 open postings total**,
  both "Senior Officer" at Bidadi — a real, working, but essentially inert
  manufacturing-HR board with zero software-engineering titles or hiring
  pipeline. Not onboarded — same class of finding as Nintex/Manulith
  elsewhere in this repo (a real, reachable board whose current dataset is
  genuinely empty of anything a software-engineering watcher should alert
  on), not a fetcher defect.
- **Toyota Connected India Pvt Ltd** (`toyotaconnected.co.in/careers`), the
  actual India software-engineering arm (Generative AI/ML/cloud/data teams
  in Chennai and **Bengaluru** — NOT Pune, and Chennai is already excluded
  by this repo's `default_exclude_locations`), links to a Darwinbox tenant:
  `toyotaconnected.darwinbox.in`. This is the newer "candidatev2" Darwinbox
  front-end (`new_careers: true` in the tenant's own `getCompanyConfig`
  response, unlike `zomato_fetcher.py`'s older "candidate" v1 tenant) —
  **onboarded here.**

**Why Playwright is required (unlike Zomato's plain-`requests` Darwinbox
integration):** this specific tenant sits behind a Cloudflare bot-challenge
(`Attention Required! | Cloudflare` HTML returned to every plain
`requests`/`curl` call against `*.darwinbox.in/ms/candidateapi/*` on this
tenant, confirmed with full browser-matching headers) — a real Firefox
session executing the page's own JS passes transparently and gets real
JSON, same class of finding as BNP Paribas/Tech Mahindra elsewhere in this
repo. Endpoint (found via live network capture, not guessed):

    GET https://toyotaconnected.darwinbox.in/ms/candidateapi/job/alljobs?companyId=main
        -> {"status": "success", "data": [ {...job...}, ... ]}

This call only succeeds with the session/referrer state a real page
navigation establishes first (a cold direct hit to this URL 422s with
`"no job found with that id"` even under Firefox) — so this fetcher
navigates to the candidatev2 "Open Jobs" listing page first and intercepts
the network response, rather than calling the API URL directly.

**Full description is inline** — each job's own `jd` field (HTML,
HTML-entity-escaped) is the complete JD body, no separate detail-page fetch
needed; `fetch_job_description` serves from the in-module cache filled
during the same page load.

**Confirmed NOT Pune-only** — `job/newfilters`'s own `location` facet lists
exactly `Chennai, Tamil Nadu, India` / `Bangalore, Karnataka, India` /
`Remote`; no Pune facet exists at all on this tenant. Live pool is tiny (2
jobs company-wide as of 2026-09-14: "Privacy Lead Engineer" at
Chennai+Bangalore, and "Technical Project Manager" at Bangalore), and both
explicitly require 10-15 / 12-20 years of experience — genuinely 0 matches
expected under this repo's `_MAX_EXPERIENCE_YEARS = 10` cutoff, not a
fetcher defect. (Note: the second job's `designation_display_name` field is
blank on this tenant — the fetcher falls back to `designation_name`, which
is populated for every observed job.)
"""

from __future__ import annotations

import atexit
import html as html_mod
import json
import re

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_HOME_URL = "https://toyotaconnected.darwinbox.in/ms/candidatev2/main/careers/home"
_DETAIL_BASE = "https://toyotaconnected.darwinbox.in/ms/candidatev2/main/careers/jobDetails"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


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
    # Darwinbox's `jd` field is HTML-entity-escaped HTML (e.g. "&lt;p&gt;")
    unescaped_once = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", unescaped_once)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _locations_from(job: dict) -> str:
    locs = [l.strip().rstrip(",").strip() for l in (job.get("officelocations_area") or []) if l and l.strip()]
    # Field carries a stray "\r" before ", India" in some entries.
    locs = [re.sub(r"\r", "", l) for l in locs]
    loc_str = "; ".join(locs) if locs else ""
    if not loc_str:
        return ""
    if not re.search(r"\bindia\b", loc_str, re.IGNORECASE):
        loc_str = f"{loc_str}, India"
    return loc_str


_cache: list[dict] = []
_descriptions: dict[str, tuple[str, str]] = {}
_cache_filled: bool = False


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    _ensure_browser()

    captured: list[str] = []

    def on_res(res):
        if "job/alljobs" in res.url:
            try:
                captured.append(res.text())
            except Exception:
                pass

    try:
        page = _browser.new_page(user_agent=_UA)
        page.on("response", on_res)
        page.goto(_HOME_URL, wait_until="networkidle", timeout=max(timeout, 45) * 1000)
        try:
            page.click("text=Open Jobs", timeout=8000)
        except PWTimeoutError:
            pass
        page.wait_for_timeout(4000)
        page.close()
    except Exception as exc:
        raise RateLimitError(f"Toyota Connected page load failed: {exc}") from exc

    if not captured:
        raise RateLimitError("Toyota Connected: job/alljobs response never captured")

    try:
        payload = json.loads(captured[-1])
    except ValueError as exc:
        raise RateLimitError(f"Toyota Connected: invalid JSON — {exc}") from exc

    collected: list[dict] = []
    for j in payload.get("data", []):
        job_id = str(j.get("id") or j.get("_id") or "").strip()
        title = (j.get("designation_display_name") or j.get("designation_name") or "").strip()
        if not (job_id and title):
            continue

        loc_str = _locations_from(j)
        if not loc_str:
            continue

        posting_date = (j.get("created_at") or j.get("createdAt") or "")[:10]
        app_url = f"{_DETAIL_BASE}/{job_id}"

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_str,
            "posting_date": posting_date,
            "application_url": app_url,
        })
        _descriptions[job_id] = (_strip_html(j.get("jd") or ""), posting_date)

    _cache[:] = collected
    print(f"[Toyota Connected] Cache filled: {len(collected)} jobs company-wide")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Toyota Connected India postings from the cached pool.

    keyword/location are accepted for interface compatibility but ignored —
    the whole company-wide pool is tiny (2 jobs at investigation time) and
    is cached once via a single Playwright page load.
    """
    _fill_cache(timeout=timeout)
    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) from the in-module cache filled
    during fetch_jobs() — the full JD is already embedded in the
    job/alljobs response, no separate per-job request needed.
    """
    job_id = (application_url or "").rstrip("/").rsplit("/", 1)[-1]
    return _descriptions.get(job_id, ("", ""))
