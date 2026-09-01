"""Fetches BNP Paribas India Solutions (ISPL) job listings.

ISPL's own careers hub (apac.bnppispl.com) has no search of its own — its
"Our Job Offers" links point out to the global corporate careers site
(group.bnpparibas), filtered to the "BNP Paribas India Solutions" legal
entity and a specific city.

ATS: Custom in-house (Symfony-based) career site, fronted by Akamai. This is
NOT a JS-challenge-then-cookie situation like IBM/Honeywell — Akamai here
rejects *every* plain HTTP request outright with "Access Denied", including
a bare GET of robots.txt, regardless of User-Agent or headers. A real
browser (headless Firefox via Playwright) passes cleanly on every path
tested. So both search and description fetches drive Firefox; there is no
plain-`requests` fallback for this company at all.

Search: the visible "See more job offers" button fires an XHR discovered by
capturing real network traffic:
    GET /en/careers/all-job-offers?json=1&page={n}
        &form[hint]=&form[q]="BNP Paribas India Solutions"
        &search_location=&form[city][]={city_id}&form[coordinates]=
    Header `X-Requested-With: XMLHttpRequest` is REQUIRED — every param
    above present but that header missing still 404s (the route dispatches
    on the header, not on `json=1` alone). Response body is
    `{"html": "<li>...card fragments...</li>..."}` — HTML embedded in JSON,
    not a structured job schema; parsed with regex for href/title.

The site's own `q` phrase filter is NOT a scoped employer filter when used
without a city facet: it text-matches broadly across BNP Paribas Group's
entire global job pool (verified — an unscoped query for the exact quoted
phrase "BNP Paribas India Solutions" still pulled back Vienna/Milan/Warsaw/
Manila postings with zero India connection). So a per-city facet is
mandatory here, not an optimization. ISPL's three delivery centres are
Mumbai, Chennai, and Bengaluru; only two carry open reqs today — Mumbai
(city facet id 379, ~491 jobs) and Bengaluru (id 1591, ~103 jobs). Chennai
currently returns 0 (verified independently) and is excluded by this
watcher's own policy anyway, so it's intentionally omitted — if ISPL ever
opens a 4th city, this list needs a manual update (same tradeoff as the
hardcoded India WID/city-ID lists already used for Barclays/Wells Fargo/
Mastercard elsewhere in this repo).

Location quirk (worth flagging): BNP Paribas' own list-card HTML has a
genuine data-quality bug — a large fraction of real Mumbai postings carry
the state field "Tamil Nadu" instead of "Maharashtra" (literal text
"Mumbai, Tamil Nadu, India" — Mumbai is nowhere near Tamil Nadu). Since
`matcher.py`'s `exclude_locations` rejects any location containing the
substring "Tamil Nadu" (to genuinely exclude Chennai/TN postings), passing
that raw text through would silently drop roughly half of all real Mumbai
matches. Fixed at the source: because jobs are fetched per explicit city
facet, the city is already known ground truth here, so this fetcher
discards BNPP's own state text and emits a clean "{City}, India" string
instead of trusting the site's own location field.

Job-detail pages (`/en/careers/job-offer/{slug}`) carry a clean schema.org
JobPosting JSON-LD block with `datePosted` (already ISO `YYYY-MM-DD`, no
parsing needed) and `description` as inline HTML — stripped with
BeautifulSoup before being handed to the shared skill matcher.
"""
from __future__ import annotations

import atexit
import json
import re
import time
import urllib.parse

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_ORIGIN = "https://group.bnpparibas"
_LIST_URL = f"{_ORIGIN}/en/careers/all-job-offers"
_ORG_FILTER = '"BNP Paribas India Solutions"'

# (city facet id, clean city name) — see module docstring for why Chennai
# (currently 0 open reqs, and excluded by policy regardless) is omitted.
_INDIA_CITIES = ((379, "Mumbai"), (1591, "Bengaluru"))
_MAX_PAGES_PER_CITY = 60  # safety cap; observed pools need 51 and 12

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

_FETCH_JS = """async (url) => {
    const resp = await fetch(url, {
        credentials: 'include',
        headers: {'X-Requested-With': 'XMLHttpRequest'}
    });
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""


class RateLimitError(Exception):
    """Raised when the portal is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox required, Akamai rejects plain requests outright
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
# Job-list cache — scrape once per process, serve slices on every fetch_jobs()
# ---------------------------------------------------------------------------

_jobs_cache: list[dict] = []
_cache_filled: bool = False


def _build_search_url(city_id: int, page: int) -> str:
    params = {
        "json": "1",
        "page": page,
        "form[hint]": "",
        "form[q]": _ORG_FILTER,
        "search_location": "",
        "form[city][]": city_id,
        "form[coordinates]": "",
    }
    return f"{_LIST_URL}?{urllib.parse.urlencode(params)}"


def _parse_cards(html_fragment: str) -> list[tuple[str, str]]:
    """Extract (href, title) pairs from a page of job-card HTML."""
    hrefs = re.findall(r'href="(/en/careers/job-offer/[^"]+)"', html_fragment)
    titles = re.findall(r'<h3 class="title-4">(.*?)</h3>', html_fragment, re.DOTALL)
    pairs = []
    for href, title in zip(hrefs, titles):
        clean_title = BeautifulSoup(title, "html.parser").get_text(strip=True)
        pairs.append((href, clean_title))
    return pairs


def _scrape_all_india_jobs(timeout: int = 30) -> list[dict]:
    """Open one Firefox session, page through both India cities' job pools."""
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    collected: list[dict] = []
    try:
        try:
            page.goto(_LIST_URL, wait_until="networkidle", timeout=timeout * 1000)
        except PWTimeoutError:
            page.goto(_LIST_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(3000)

        for city_id, city_name in _INDIA_CITIES:
            seen_hrefs: set[str] = set()
            pg = 1
            while pg <= _MAX_PAGES_PER_CITY:
                url = _build_search_url(city_id, pg)
                result = page.evaluate(_FETCH_JS, url)
                if result["status"] != 200:
                    break
                try:
                    data = json.loads(result["text"])
                except ValueError:
                    break  # past the last page — server falls back to full HTML
                cards = _parse_cards(data.get("html", "") or "")
                if not cards:
                    break
                new_any = False
                for href, title in cards:
                    if href in seen_hrefs or not title:
                        continue
                    seen_hrefs.add(href)
                    new_any = True
                    job_id = href.rstrip("/").rsplit("/", 1)[-1]
                    collected.append({
                        "id": job_id,
                        "title": title,
                        "location": f"{city_name}, India",
                        "posting_date": "",  # filled in on description fetch
                        "application_url": f"{_ORIGIN}{href}",
                    })
                if not new_any:
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
    """Return a cached slice of BNP Paribas India Solutions jobs.

    The site's own keyword search is not scoped to India without a city
    facet (see module docstring), so `keyword`/`location` are ignored; the
    full India pool across both cities is scraped once and cached, then
    served in slices so matcher.py's own pagination loop works normally.
    """
    global _jobs_cache, _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before attempting — avoid a retry storm
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                _jobs_cache = _scrape_all_india_jobs(timeout=timeout)
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            raise RateLimitError(f"BNP Paribas cache fill failed: {last_exc}")
        print(f"[BNP Paribas] Cache filled: {len(_jobs_cache)} India jobs")

    return _jobs_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) parsed from the detail page's JSON-LD."""
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
                r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
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
                f"BNP Paribas description fetch failed: {exc}"
            ) from exc
        finally:
            page.close()
            context.close()

    raise RateLimitError(f"BNP Paribas description fetch failed: {last_exc}")
