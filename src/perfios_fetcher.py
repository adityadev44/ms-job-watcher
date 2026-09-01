"""Fetches Perfios job listings — Darwinbox candidatev2 ATS, Cloudflare-gated.

ATS discovery (2026-08-31): perfios.com/careers redirects to
perfios.ai/careers, which embeds `https://perfios.darwinbox.in/ms/
candidate/careers` — Darwinbox, tenant "perfios" (a fourth Darwinbox
tenant in this repo, alongside Hexaware/Chubb/Icertis-style Oracle... no,
alongside Zomato/Zepto/Hetero). Unlike Zomato's "eternal" tenant (which
runs Darwinbox's older `new_careers:false` front-end and answers plain
`requests.get()` calls with no protection), Perfios's tenant has
`new_careers:true` — the newer "candidatev2" SPA — AND sits behind
Cloudflare bot management that returns a bare HTTP 403 to every plain
`requests` call (even with full browser headers/Referer replicated
exactly from a real session). A real browser passes cleanly. So, like
Honeywell/IBM/BNP Paribas elsewhere in this repo, both search and
description are fetched via headless Firefox (Playwright).

Real API endpoints (found via live Playwright network capture of
`/ms/candidatev2/main/careers/allJobs`, then clicking through to a job
detail page):

    GET /ms/candidateapi/job/alljobs?companyId=main
        -> {"status":"success","data":[{id, title, jd (HTML, INLINE!),
             officelocation_show_arr, country, posted_on, ...}]}
    GET /ms/candidateapi/job/{id}?companyId=main
        -> per-job detail (same shape) — not needed here since `alljobs`
           already returns the full `jd` HTML for every posting in one call.

No keyword/pagination params exist (small board) — the whole pool is
fetched and cached once per process via a single `page.evaluate(fetch(...))`
call inside an already-loaded Cloudflare-cleared browser context, same
`context.request`-via-`page.evaluate` idiom as `bnpparibas_fetcher.py`.

**Current live state — genuinely 3 open postings, ALL Customer Success,
zero engineering, not a fetcher bug:** confirmed via both the rendered
page text ("We Have 3 Open Jobs") and the raw API response. This is the
same "confirmed-zero-is-a-real-fact" precedent as ING/eClerx/Zomato
elsewhere in this repo — the pipeline is mechanically correct and will
pick up new India engineering postings automatically the moment Perfios
opens one, no code change needed.

Location: `officelocation_show_arr` is already a clean "City, State,
India" string on every observed posting — no substring guessing needed.

Description: `jd` is HTML-entity-escaped one extra level (raw text starts
`&lt;p&gt;...`), same idiom as Razorpay/Groww/Zomato/M2P — unescape, strip
tags, unescape again.

Application URL:
`https://perfios.darwinbox.in/ms/candidatev2/main/careers/jobDetails/{id}
?from=all` — confirmed live via Playwright click-through.
"""
from __future__ import annotations

import atexit
import html as html_mod
import re
import time
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_TENANT = "perfios"
_BASE = f"https://{_TENANT}.darwinbox.in"
_CAREERS_PAGE = f"{_BASE}/ms/candidatev2/main/careers/allJobs"
_ALLJOBS_API = f"{_BASE}/ms/candidateapi/job/alljobs?companyId=main"
_JOB_PAGE_BASE = f"{_BASE}/ms/candidatev2/main/careers/jobDetails/"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

_FETCH_JS = """async (url) => {
    const resp = await fetch(url, {credentials: 'include'});
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


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


# ---------------------------------------------------------------------------
# Browser singleton — Firefox required, Cloudflare rejects plain requests
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
# Job-list cache — scrape once per process, serve slices on every fetch_jobs()
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


def _scrape_all_jobs(timeout: int = 30) -> list[dict]:
    """Open one Firefox session and capture the SPA's OWN `alljobs` XHR
    response during natural page load, rather than replaying the request
    afterward via a separate in-page fetch().

    A manually-triggered `page.evaluate(fetch(url))` call to this same URL
    returns HTTP 422 ("no job found with that id") even though the URL/params
    are byte-identical to what the page's own JS calls — confirmed via a live
    network capture that the real page load succeeds against this exact
    endpoint. The SPA's own request framework must attach something (a
    short-lived token/header from an earlier getCompanyConfig/getAmplifyScript
    call, or simply request *timing/ordering*) that a same-URL manual re-fetch
    doesn't reproduce. Capturing the response Playwright already observes
    sidesteps the problem entirely instead of trying to replicate it.
    """
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    captured: dict = {}

    def _on_response(resp):
        if "candidateapi/job/alljobs" in resp.url and "status" not in captured:
            try:
                captured["status"] = resp.status
                captured["body"] = resp.text()
            except Exception as exc:
                captured["error"] = str(exc)

    page.on("response", _on_response)

    collected: list[dict] = []
    try:
        try:
            page.goto(_CAREERS_PAGE, wait_until="networkidle", timeout=timeout * 1000)
        except PWTimeoutError:
            page.goto(_CAREERS_PAGE, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(3000)

        if not captured or captured.get("status") != 200:
            # Fallback: the natural load's own XHR wasn't observed (SPA route
            # changed, or fired before the listener attached) -- try the
            # manual re-fetch as a second attempt rather than failing outright.
            result = page.evaluate(_FETCH_JS, _ALLJOBS_API)
            if result["status"] != 200:
                raise RateLimitError(
                    f"Perfios alljobs API returned HTTP {result['status']} "
                    f"(captured={captured.get('status')})"
                )
            captured["body"] = result["text"]

        import json
        data = json.loads(captured["body"])
        raw_jobs = (data or {}).get("data") or []

        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or j.get("designation_name") or "").strip()
            if not (job_id and title):
                continue
            collected.append({
                "id": job_id,
                "title": title,
                "location": (j.get("officelocation_show_arr") or "India").strip() or "India",
                "posting_date": _epoch_to_date(j.get("posted_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}?from=all",
            })
            _desc_cache[job_id] = _strip_html(j.get("jd") or "")
    finally:
        page.close()
        context.close()

    return collected


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a cached slice of Perfios (Darwinbox) postings.

    keyword/location are accepted but ignored — this is a small board with
    no server-side filtering worth relying on; the shared matcher does the
    real title/skill/India filtering. The whole board is scraped once via
    headless Firefox (Cloudflare-gated) and cached.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before attempting — avoid a retry storm
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                _job_cache[:] = _scrape_all_jobs(timeout=timeout)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            raise RateLimitError(f"Perfios cache fill failed: {last_exc}")
        print(f"[Perfios] Cache filled: {len(_job_cache)} total jobs")

    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Perfios job.

    Served entirely from the cache filled by fetch_jobs() — the alljobs
    API already returns the full `jd` HTML for every posting in one call.
    """
    job_id = application_url.rstrip("/").split("?", 1)[0].rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
