"""Fetches NatWest Group job listings via jobs.natwestgroup.com.

ATS: Talemetry (a career-site/CRM product now owned by iCIMS, but a
distinct URL/JSON shape from the classic iCIMS integrations already in this
repo — confirmed via the page's own JS bundle path
`/pack/talemetry_careersites/index.*.js` and `window.talemetry`). New ATS
vendor for this repo.

Bot protection: the entire `jobs.natwestgroup.com` domain sits behind a
Cloudflare "managed challenge" (Turnstile, `cType: 'managed'`) that blocks
every plain `requests` call outright — even `robots.txt` returns HTTP 403
with `cf-mitigated: challenge`. Headless **Firefox** via Playwright gets
stuck on the "Just a moment..." interstitial indefinitely (tested 30s, never
resolves — Cloudflare's JS fingerprinting flags it). Headless **Chromium**
launched with `--disable-blink-features=AutomationControlled` passes the
challenge cleanly on a cold top-level navigation (`page.goto()` returns 200
immediately) — the opposite lesson from Honeywell/IBM/Tech Mahindra (Firefox
required there), matching Flipkart's precedent instead (Chromium over
Firefox). So both search and description fetches drive headless Chromium.

Rate-based re-challenge: firing several navigations back-to-back in the same
browser (even across fresh contexts, ~3s apart) gets re-challenged after the
very first request — Cloudflare's bot score reacts to request *frequency*,
not just per-request fingerprint. A fresh `browser.new_context()` per
request plus a enforced minimum ~5s gap between any two navigations to this
domain (`_throttle()`) was verified reliable across repeated live tests;
without it, every request after the first came back as another challenge
page with 0 usable content.

Search: `GET /search/jobs/in/country/india.json?search_type=talemetry&per_page=100`
returns the *entire* India pool (65 jobs observed) in one call —
`{"current_page", "per_page", "total_entries", "entries": [...]}`, each entry
carrying `id`, `permalink`, `title`, and a `location` object with
`locality`/`region_full`/`country`. Entry IDs are strictly descending
(newest first). The site also has a `q=` relevance search
(`/search/jobs.json?location=<city>&q=<term>`), but it's a loose OR-style
match (e.g. `q=python` surfaced "Risk Quants Senior Analyst" and "Financial
Control Analyst" — no Python connection) — not safe to rely on, and with a
pool this small there's no efficiency reason to. So `fetch_jobs()` caches
the whole India pool once per process and ignores `keyword`/`location`,
same pattern as Societe Generale/Deutsche Bank/BNP Paribas; matcher.py's own
title/skill filters do the real narrowing.

Job-detail pages (`/jobs/{id}-{permalink}`) carry two
`<script type="application/ld+json">` blocks — an `Organization`/`@graph`
block first, then a direct `JobPosting` dict (order not guaranteed, so both
are scanned). `description` is inline HTML (stripped with BeautifulSoup);
`datePosted` is already `YYYY-MM-DD`. A job's `JobPosting` node can be
entirely absent if the posting closed between the search fetch and the
detail fetch (India roles here churn fast, as with Infosys/TCS) — handled by
returning `("", "")` rather than raising.

Real-data finding worth flagging: some titles that explicitly name a stack
("Python Software Engineer, VP") have a *generic* corporate-template body
that never repeats the stack name at all — the title carries stronger
tech signal than the description here, the inverse of the IT-services
generic-title problem `require_tech_in_description` (Layer 4) was built
for. Enabling Layer 4 for NatWest would risk dropping genuine matches, not
just tightening precision, so it is intentionally NOT enabled — see the
registry-row report for the full reasoning.
"""
from __future__ import annotations

import atexit
import json
import re
import time

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_ORIGIN = "https://jobs.natwestgroup.com"
_SEARCH_URL = f"{_ORIGIN}/search/jobs/in/country/india.json"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1280, "height": 800}

# Minimum gap enforced between any two navigations to jobs.natwestgroup.com —
# Cloudflare's managed challenge re-triggers on request frequency, verified
# live (see module docstring). 5s was reliable across 3 back-to-back tests;
# kept slightly above that for margin.
_MIN_GAP_SECONDS = 6.0
_last_request_ts: float = 0.0


class RateLimitError(Exception):
    """Raised when the portal is unreachable, challenged, or Playwright is unavailable."""


# ---------------------------------------------------------------------------
# Browser singleton — Chromium required (Firefox never clears the Cloudflare
# managed challenge here; see module docstring)
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
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
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


def _throttle() -> None:
    """Enforce a minimum gap since the last navigation to this domain."""
    global _last_request_ts
    now = time.monotonic()
    wait = _MIN_GAP_SECONDS - (now - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()


def _new_page():
    context = _browser.new_context(user_agent=_UA, viewport=_VIEWPORT, locale="en-US")
    return context, context.new_page()


# ---------------------------------------------------------------------------
# Job-list cache — fetch the whole India pool once per process
# ---------------------------------------------------------------------------

_jobs_cache: list[dict] = []
_cache_filled: bool = False


def _location_string(loc: dict) -> str:
    locality = (loc or {}).get("locality") or ""
    if locality:
        return f"{locality}, India"
    return "India"


def _fetch_india_page(page_num: int, timeout: int) -> dict:
    url = f"{_SEARCH_URL}?search_type=talemetry&per_page=100&page={page_num}"
    _throttle()
    context, pg = _new_page()
    try:
        resp = pg.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        if resp is None:
            raise RuntimeError("no response from search endpoint")
        if resp.status != 200:
            raise RuntimeError(f"search endpoint returned HTTP {resp.status}")
        text = resp.text()
        return json.loads(text)
    finally:
        pg.close()
        context.close()


def _fill_cache(timeout: int = 20) -> None:
    global _jobs_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before attempting — avoid a retry storm

    _ensure_browser()

    collected: list[dict] = []
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            collected = []
            page_num = 1
            while True:
                data = _fetch_india_page(page_num, timeout)
                entries = data.get("entries", [])
                if not entries:
                    break
                for e in entries:
                    job_id = str(e.get("id", ""))
                    permalink = e.get("permalink", "")
                    if not job_id or not permalink:
                        continue
                    collected.append({
                        "id": job_id,
                        "title": e.get("title", ""),
                        "location": _location_string(e.get("location")),
                        "posting_date": "",  # filled in on description fetch
                        "application_url": f"{_ORIGIN}/jobs/{job_id}-{permalink}",
                    })
                total = data.get("total_entries", len(collected))
                if len(collected) >= total or len(entries) < data.get("per_page", 100):
                    break
                page_num += 1
            break
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"NatWest Group cache fill failed: {exc}") from exc

    if not collected and last_exc is not None:
        raise RateLimitError(f"NatWest Group cache fill failed: {last_exc}")

    _jobs_cache = collected
    print(f"[NatWest Group] Cache filled: {len(_jobs_cache)} India jobs")


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
    """Return a cached slice of NatWest Group India jobs.

    The site's own `q=` relevance search is a loose OR-match, not a reliable
    scoped filter (see module docstring), and the whole India pool is small
    enough to fetch once — so `keyword`/`location` are ignored here;
    matcher.py's own title/skill filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _jobs_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) parsed from the detail page's JSON-LD."""
    _ensure_browser()

    last_exc: Exception | None = None
    for attempt in range(3):
        _throttle()
        context, page = _new_page()
        try:
            try:
                resp = page.goto(application_url, timeout=timeout * 1000, wait_until="domcontentloaded")
            except PWTimeoutError as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    f"NatWest Group description fetch timed out: {exc}"
                ) from exc

            if resp is None or resp.status != 200:
                status = resp.status if resp else "no response"
                last_exc = RuntimeError(f"detail page returned {status}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    f"NatWest Group description fetch failed: HTTP {status}"
                )

            page.wait_for_timeout(2000)
            html = page.content()
            blocks = re.findall(
                r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
            )
            job_posting = None
            for block in blocks:
                try:
                    data = json.loads(block)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    job_posting = data
                    break
                if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                    for node in data["@graph"]:
                        if isinstance(node, dict) and node.get("@type") == "JobPosting":
                            job_posting = node
                            break
                    if job_posting:
                        break

            if not job_posting:
                # Posting likely closed between search and detail fetch
                # (observed live — India roles here churn fast).
                return "", ""

            desc_html = job_posting.get("description", "") or ""
            text = BeautifulSoup(desc_html, "html.parser").get_text(separator=" ", strip=True)
            posted = (job_posting.get("datePosted") or "").strip()
            return text, posted
        except RateLimitError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"NatWest Group description fetch failed: {exc}"
            ) from exc
        finally:
            page.close()
            context.close()

    raise RateLimitError(f"NatWest Group description fetch failed: {last_exc}")
