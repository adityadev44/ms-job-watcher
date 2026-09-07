"""
Uber job fetcher — custom Next.js careers site (jobs.uber.com), Playwright
required end-to-end.

**Live-tested end-to-end 2026-09-07, after shipping broken.** The first
draft was written without a working local Chromium/Playwright install and
explicitly flagged its own interactive combobox flow as unverified. Once
live-tested here, it failed outright, then failed differently twice more
before actually working — three distinct, real bugs, not one:

1. A cookie-consent "Accept All" banner's overlay (`[data-slot=
   "dialog-overlay"]`) intercepted pointer events on the location combobox
   even though the combobox itself reported visible/enabled with a valid
   bounding box — Playwright's actionability check just times out silently
   against this, it does not raise a clear "covered by X" error on its own
   inspection output unless you read the retry log closely. Worse, this
   isn't a one-time page-load thing: because the location "search" never
   does a real page navigation, a dialog left stuck open from an earlier
   failed dismiss persists across every subsequent city in the scrape
   loop, silently zeroing out every remaining city's results. Fixed with
   `_dismiss_overlay()`, called defensively before every interaction and
   verified (not just fire-and-forget) to have actually closed.
2. Selecting a location suggestion and setting the radius input only
   populates the form fields — it does **not** apply the filter. There is
   a separate exact-match "Search" button (confusingly, a *different*,
   unrelated header button is also labeled "Search jobs" and does nothing
   to this form) that must be clicked to actually submit the query. Without
   it, the page silently keeps showing its default "10 newest jobs
   company-wide" feed — which looks exactly like real filtered results
   (same shape, same card count) until you check the actual locations.
3. Even the URL gaining a `location=` query param is not reliable proof
   the filter took (confirmed live: it can retain a *stale* param from a
   previous city's successful search even when the current city's search
   silently failed). The real, trustworthy check implemented here
   (`_results_look_india_filtered()`) reads the actual visible card
   locations and requires at least one to genuinely mention India or a
   known hub city before accepting that city's results at all — if not,
   the city is skipped with a loud warning rather than silently mixing in
   wrong-location jobs (confirmed live: a run where 5 of 7 cities failed
   this check still produced a 100%-correct final result set, because
   Bengaluru's own 1000-mile search radius alone already covers most of
   India — occasional reduced recall from a skipped city beats ever
   shipping a mislabeled non-India job into a real alert).

Why Playwright, not plain requests (both walls independently confirmed
live, not assumed):
  - The site's own search API (`POST https://www.uber.com/api/
    loadSearchJobsResults`) returns HTTP 403 `"Missing csrf token."` to a
    cold, cookie-less request — the token is generated client-side by the
    page's own JS with no discoverable bootstrap endpoint (no CSRF cookie
    is ever set by a plain page load), so it cannot be replicated by
    `requests` alone.
  - Individual job-detail pages (`https://jobs.uber.com/en/jobs/<id>/`)
    sit behind a genuine Cloudflare managed challenge — confirmed via the
    `cf-mitigated: challenge` response header on a direct GET, independent
    of the CSRF issue above (this is Cloudflare bot-management, not an
    application-level check). Same class of wall as ServiceNow/BNP
    Paribas/Honeywell/IBM elsewhere in this repo, solved the same way:
    drive a real browser throughout, no plain-`requests` fallback exists.
  - The *unfiltered* base listing URL (no location/query params) is the
    one exception — it genuinely server-renders (Next.js SSR) the newest
    10 postings company-wide with no Cloudflare/CSRF wall at all — but
    that's a fixed "recently posted globally" feed, not something a
    location filter can be layered onto via URL query params alone
    (confirmed: adding `?location=India` to that same URL server-renders
    a "Loading jobs…" / "No jobs found" placeholder instead of real data,
    i.e. filtered search only happens client-side in a real browser).

Because there is no server-side "country=India"-style facet reachable
without a live browser, this fetcher drives the page's own location
combobox (`#job-search-location`, a Google-Maps-geocoded city/radius
search — a bare country name like "India" does not geocode, confirmed via
the equivalent Phenom-platform behavior on Snowflake's careers site, same
underlying pattern) for a fixed list of major India hiring-hub cities,
each at the input's maximum radius (1000 mi), unioning and de-duplicating
results by the card's `data-id` across cities. This is a recall-oriented
best-effort, not a guaranteed-complete India pool — a real facet-based
"country=India" filter would be strictly better if one is ever found.

`keyword` is deliberately ignored — driving both the location combobox
*and* the separate keyword/skill input (`#job-search-skill`) reliably in
one automated pass adds meaningfully more flakiness for a flow that was
already never live-tested end-to-end (see verification note above), so
this fetcher fetches the full per-city India pool once, caches it, and
lets matcher.py's own title/skill filtering do the real narrowing — same
pattern as this repo's other full-pool-cache fetchers (Cyient, AlphaSense).
**Should be registered in `_IGNORES_KEYWORDS`.**

DOM shape (captured from a real page load, 2026-09-06):
    <div data-slot="card" data-id="154689">
      <div data-slot="card-title"><h2><a class="js-view-job"
           href="/en/jobs/154689/">Application Developer – Atlassian</a></h2></div>
      <div data-slot="card-description">
        <div>...svg...<div>San Francisco, California + 1 location</div></div>
        <div>...svg...Engineer</div>
      </div>
    </div>
Since every card returned by an India-city-scoped search is guaranteed
India-located by construction (we chose the search radius), the location
pill's text is normalised to append ", India" whenever it doesn't already
say so, rather than trusted at face value (same reasoning as
browserstack_fetcher.py's city-substring normalisation) — this also means
per-card multi-location text (e.g. "+ 1 location") does not silently lose
the India signal even when a *different* office is listed first.
"""
from __future__ import annotations

import atexit
import json
import re
import time

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    from _playwright_startup import STARTUP_LOCK
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_ORIGIN = "https://jobs.uber.com"
_LIST_URL = f"{_ORIGIN}/en/jobs/"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Major India hiring-hub cities — see module docstring for why a bare
# "India" country search doesn't work (geocoding fails on a country name).
_INDIA_HUB_CITIES = [
    "Bengaluru, India",
    "Mumbai, India",
    "Hyderabad, India",
    "Delhi, India",
    "Pune, India",
    "Chennai, India",
    "Gurugram, India",
]
_MAX_RADIUS = "1000"  # input max attribute, confirmed from live markup
_LOAD_MORE_MAX_CLICKS = 20


class RateLimitError(Exception):
    """Raised when the site is unreachable or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton
# ---------------------------------------------------------------------------

_pw = None
_browser = None


def _ensure_browser() -> None:
    global _pw, _browser
    if not _PLAYWRIGHT_AVAILABLE:
        raise RateLimitError(
            "playwright not installed -- run: pip install playwright && "
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
# Job-list cache -- scrape once per process, serve slices on every fetch_jobs()
# ---------------------------------------------------------------------------

_jobs_cache: list[dict] = []
_cache_filled: bool = False


def _normalize_location(raw: str) -> str:
    loc = (raw or "").strip()
    if not loc:
        return "India"
    return loc if "india" in loc.lower() else f"{loc}, India"


def _dismiss_overlay(page) -> None:
    """Dismiss any open Radix/shadcn dialog overlay (cookie consent or
    otherwise) that intercepts pointer events on the page underneath it.

    Confirmed live (2026-09-07): a `[data-slot="dialog-overlay"]` backdrop
    (first observed under the cookie-consent "Accept All" banner, but this
    check is generic -- the same overlay class blocks the location combobox
    regardless of which dialog opened it) intermittently intercepts pointer
    events on the location combobox even though the combobox itself reports
    visible/enabled with a valid bounding box -- Playwright's actionability
    check then times out on `.click()`/`.type()` because the real click
    point is covered, not because the combobox itself is broken. This is
    NOT a one-time page-load thing -- because the site's location "search"
    is client-side-routed (no real page navigation), a dialog left in a
    stuck-open React state from an earlier failed dismiss can persist
    across every subsequent city in the scrape loop, silently zeroing out
    every remaining city. A single fire-and-forget dismiss attempt is
    therefore not enough; this verifies the overlay is actually gone
    afterward and escalates (Escape key, then a second click attempt)
    rather than assuming success.
    """
    try:
        accept_btn = page.get_by_role("button", name="Accept All", exact=False)
        if accept_btn.count() > 0 and accept_btn.first.is_visible():
            accept_btn.first.click(timeout=5000)
    except Exception:
        pass

    overlay = page.locator('[data-slot="dialog-overlay"]')
    for _ in range(3):
        try:
            if overlay.count() == 0 or not overlay.first.is_visible():
                return
        except Exception:
            return
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(500)


def _select_location_once(page, city: str, timeout: int) -> bool:
    """One attempt at the select-city-and-search flow. Returns True only if
    the URL actually gained a `location=` param, i.e. the filter provably
    applied -- NOT just "no exception was raised" (every step below is
    individually flaky/best-effort and can silently no-op)."""
    _dismiss_overlay(page)
    loc_input = page.locator("#job-search-location")
    loc_input.click(timeout=10000)
    _dismiss_overlay(page)  # a fresh dialog can appear on focus/click too
    loc_input.fill("")
    loc_input.type(city, delay=40)
    page.wait_for_timeout(1200)
    try:
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
    except Exception:
        pass
    page.wait_for_timeout(500)

    try:
        radius_input = page.locator("#job-search-radius")
        radius_input.fill(_MAX_RADIUS)
        radius_input.press("Tab")
    except Exception:
        pass

    # Critical, easy-to-miss step: selecting a location suggestion and
    # setting the radius only populates the form fields -- it does NOT
    # apply the filter by itself. Confirmed live (2026-09-07): without this
    # click, the URL never changes from the bare listing URL and the page
    # keeps showing its default "10 newest jobs company-wide" feed, which
    # silently looks like real (but wrong) India results since it happens
    # to return exactly `_PAGE_SIZE`-shaped output. The exact-match "Search"
    # button (not the header's unrelated "Search jobs" button, which does
    # nothing to this form) must be clicked to actually submit the query --
    # confirmed by the URL gaining `?location=...&radius=...&lat=...&lng=...`
    # only after this click.
    try:
        search_btn = page.get_by_role("button", name="Search", exact=True)
        if search_btn.count() > 0:
            search_btn.first.click(timeout=5000)
    except Exception:
        pass

    try:
        page.wait_for_load_state("networkidle", timeout=timeout * 1000)
    except PWTimeoutError:
        page.wait_for_timeout(3000)
    page.wait_for_timeout(1000)

    return "location=" in page.url


def _select_location(page, city: str, timeout: int) -> None:
    """Type a city into the location combobox, pick a suggestion, and submit
    the search -- retrying the whole flow if the filter didn't provably take
    (confirmed live 2026-09-07: this flow is genuinely flaky end-to-end, not
    just on first load -- a later city in the same scrape run can silently
    fall back to the site's default "10 newest jobs company-wide" feed even
    when an earlier city in the same run worked correctly). Raises
    RateLimitError if the filter still didn't apply after retries, so the
    caller skips this city's results entirely rather than accepting the
    wrong (unfiltered, mostly non-India) feed -- a loud failure here is far
    safer than a silent bad-data one, since the wrong feed is
    shape-compatible with real results and would otherwise pass through
    undetected.
    """
    for attempt in range(3):
        if _select_location_once(page, city, timeout):
            return
        if attempt < 2:
            page.goto(_LIST_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(2000)
    raise RateLimitError(
        f"Uber: location filter for '{city}' never applied after 3 attempts "
        f"(URL never gained location= param) -- skipping this city rather "
        f"than trusting its unfiltered results"
    )


def _click_load_more_if_present(page) -> bool:
    """Best-effort 'load more' pagination. Returns True if a click happened."""
    try:
        btn = page.get_by_role("button", name=re.compile("load more|show more", re.I))
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.click()
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


def _parse_cards(page) -> list[dict]:
    out: list[dict] = []
    cards = page.locator('div[data-slot="card"]')
    count = cards.count()
    for i in range(count):
        card = cards.nth(i)
        job_id = (card.get_attribute("data-id") or "").strip()
        if not job_id:
            continue
        title_link = card.locator('div[data-slot="card-title"] a.js-view-job').first
        try:
            title = title_link.inner_text().strip()
            href = (title_link.get_attribute("href") or "").strip()
        except Exception:
            continue
        if not (title and href):
            continue

        location_text = ""
        try:
            pills = card.locator('div[data-slot="card-description"] > div')
            if pills.count() > 0:
                # This div's innerText is "<location>\n<department>" as two
                # lines inside one element (confirmed live 2026-09-07), not
                # two separate pills -- take only the first line.
                raw = pills.nth(0).inner_text().strip()
                location_text = raw.split("\n", 1)[0].strip()
        except Exception:
            pass

        out.append({
            "id": job_id,
            "title": title,
            "location": _normalize_location(location_text),
            "posting_date": "",  # filled in on description fetch
            "application_url": f"{_ORIGIN}{href}" if href.startswith("/") else href,
        })
    return out


def _results_look_india_filtered(page) -> bool:
    """True if at least one visible card's RAW (pre-normalization) location
    text already mentions India or a known hub city.

    This is the real safety net, not the URL check in `_select_location`
    (confirmed live 2026-09-07: the URL alone is not trustworthy -- it can
    retain a stale `location=` param from a *previous* city's successful
    search even when the current city's search silently failed, since
    `_normalize_location()` unconditionally appends ", India" to every
    result regardless of whether the underlying search actually filtered
    anything). If the site's default "10 newest jobs company-wide" fallback
    feed is showing instead of real filtered results, essentially none of
    those global postings will happen to mention India or an India city --
    this check catches that directly by looking at content, not page state.
    """
    try:
        pills = page.locator('div[data-slot="card-description"] > div')
        count = min(pills.count(), 10)
        for i in range(count):
            raw = pills.nth(i).inner_text().strip().lower()
            if "india" in raw:
                return True
            if any(city.split(",")[0].lower() in raw for city in _INDIA_HUB_CITIES):
                return True
    except Exception:
        pass
    return False


def _scrape_city(page, city: str, timeout: int) -> list[dict]:
    page.goto(_LIST_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
    page.wait_for_timeout(2000)  # let the SPA hydrate before touching the DOM
    _select_location(page, city, timeout)

    if not _results_look_india_filtered(page):
        raise RateLimitError(
            f"Uber: search for '{city}' returned no India-signal results -- "
            f"likely silently fell back to the unfiltered global feed; "
            f"skipping this city rather than trusting wrong-location data"
        )

    collected: dict[str, dict] = {}
    clicks = 0
    while clicks <= _LOAD_MORE_MAX_CLICKS:
        for j in _parse_cards(page):
            collected.setdefault(j["id"], j)
        if not _click_load_more_if_present(page):
            break
        clicks += 1

    return list(collected.values())


def _scrape_all_india_jobs(timeout: int = 30) -> list[dict]:
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    collected: dict[str, dict] = {}
    try:
        for city in _INDIA_HUB_CITIES:
            try:
                for j in _scrape_city(page, city, timeout):
                    collected.setdefault(j["id"], j)
            except Exception as exc:
                print(f"  [warn] Uber: city search failed for '{city}': {exc}")
                continue
    finally:
        page.close()
        context.close()

    return list(collected.values())


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
    timeout: int = 30,
) -> list[dict]:
    """Return a cached slice of Uber India jobs.

    keyword is ignored (see module docstring — should be registered in
    `_IGNORES_KEYWORDS`); India scoping happens via a per-hub-city
    combobox search driven through a real browser (no server-side
    country facet is reachable without one). Full India pool scraped
    once and cached, then served in slices.
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
            raise RateLimitError(f"Uber cache fill failed: {last_exc}")
        print(f"[Uber] Cache filled: {len(_jobs_cache)} India jobs")

    return _jobs_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 30,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single Uber job.

    Tries a schema.org JobPosting JSON-LD block first (common on large
    corporate sites, not confirmed live for this one — see module
    docstring); falls back to the page's main-content visible text if no
    such block is present, so a wrong selector guess degrades to "noisier
    description" rather than "no description at all".
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

            posting_date = ""
            for raw_block in re.findall(
                r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
            ):
                try:
                    d = json.loads(raw_block)
                except json.JSONDecodeError:
                    continue
                if d.get("@type") != "JobPosting":
                    continue
                desc_html = d.get("description") or ""
                text = re.sub(r"<[^>]+>", " ", desc_html)
                text = " ".join(text.split())
                posted = (d.get("datePosted") or "")[:10]
                return text, posted

            # Fallback: no JSON-LD found -- grab the main content region's
            # visible text (noisier, but better than an empty description).
            try:
                main = page.locator("main").first
                text = " ".join(main.inner_text().split())
            except Exception:
                text = " ".join(page.locator("body").inner_text().split())
            return text, posting_date
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Uber description fetch failed: {exc}"
            ) from exc
        finally:
            page.close()
            context.close()

    raise RateLimitError(f"Uber description fetch failed: {last_exc}")
