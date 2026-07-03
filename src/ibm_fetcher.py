"""Fetches IBM India job listings via IBM's Coveo-style search API + Firefox.

careers.ibm.com's search widget calls a plain JSON REST API
(www-api.ibm.com/search/api/v2, an Elasticsearch-backed endpoint) — no
Playwright needed for search. India is filtered server-side via a
`field_keyword_05` term filter (discovered from the response's own
aggregations, which show it as the "Country" facet).

Key quirks:
- Location in search results is "{City}, IN" (bare code, not "India") —
  reconstructed as "{City}, India" since field_keyword_05 == "India" already
  guarantees a genuine India posting.
- Job detail pages (careers.ibm.com/careers/JobDetail?jobId=N) sit behind
  AWS WAF bot-challenge tokens — plain `requests` gets a 202 with an empty
  body. A real browser (Playwright/Firefox, reused from the Honeywell
  pattern) solves the challenge transparently and renders the full JD.
- No posting-date field is exposed anywhere (search or detail) — posting_date
  is left empty; matcher.py's date sort tolerates that.
"""
from __future__ import annotations

import atexit

import requests

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_SEARCH_URL = "https://www-api.ibm.com/search/api/v2"
_SEARCH_FIELDS = [
    "keywords^1", "body^1", "url^2", "description^2", "h1s_content^2",
    "title^3", "field_text_01",
]
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": "https://www.ibm.com/careers/search",
}


class RateLimitError(Exception):
    """Raised on 429, persistent network failure, or a missing browser for description fetch."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox, reused across all description fetches
# (same pattern as honeywell_fetcher.py)
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
    if not keyword:
        return []

    payload = {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {
            "bool": {
                "must": [{"simple_query_string": {"query": keyword, "fields": _SEARCH_FIELDS}}],
                "filter": [{"term": {"field_keyword_05": "India"}}],
            }
        },
        "size": num,
        "from": start,
        "sort": [{"_score": "desc"}, {"pageviews": "desc"}],
        "lang": "zz",
        "_source": ["title", "url", "field_keyword_19", "field_keyword_05"],
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(_SEARCH_URL, headers=_HEADERS, json=payload, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    import time
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("IBM: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"IBM fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"IBM fetch: no response — {last_exc}")

    jobs: list[dict] = []
    for hit in r.json().get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        url = src.get("url", "")
        title = (src.get("title") or "").strip()
        if not (url and title):
            continue

        job_id = url.rsplit("jobId=", 1)[-1] if "jobId=" in url else ""
        if not job_id:
            continue

        city = (src.get("field_keyword_19") or "").split(",")[0].strip()
        location_str = f"{city}, India" if city and city != "Multiple Cities" else "India"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": "",  # not exposed anywhere by IBM's careers site
            "application_url": url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 30,
) -> tuple[str, str]:
    """Fetch the full description via headless Firefox (WAF-gated detail page)."""
    _ensure_browser()
    context = _browser.new_context(user_agent=_UA, ignore_https_errors=True)
    page = context.new_page()

    try:
        try:
            page.goto(application_url, wait_until="networkidle", timeout=timeout * 1000)
        except PWTimeoutError:
            try:
                page.goto(application_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except PWTimeoutError:
                pass
            page.wait_for_timeout(4000)

        body = page.inner_text("body")
        # Trim the header/nav boilerplate before "Apply now"; keep everything
        # after it (JD body + the structured Job Title/City/Country fields).
        idx = body.find("Apply now")
        text = body[idx:] if idx != -1 else body
        return " ".join(text.split()), ""
    except Exception as exc:
        raise RateLimitError(f"IBM description fetch failed: {exc}") from exc
    finally:
        page.close()
        context.close()
