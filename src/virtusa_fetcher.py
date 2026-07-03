"""Fetches Virtusa job listings via Taleo (virtusa.taleo.net).

Search uses Taleo's clean REST endpoint
(careersection/rest/jobboard/searchjobs) — plain JSON in/out, but requires
TZ/tzname/X-Requested-With headers matching what the browser sends or the
server returns a raw HTTP 500 (verified: omitting any of them breaks it).
India is filtered server-side via the LOCATION facet id "200100250"
(discovered from the response's own facetResults).

Job-detail pages are a different story: the description is populated by a
`jobdetail.ajax` POST carrying ~100 JSF-style form fields (ViewState-like
state, ~13KB) in a pipe-delimited response format — far too fragile to
hand-replicate reliably. Description fetch instead drives headless Firefox
(Playwright, reused browser singleton) to the jobdetail.ftl page directly,
which renders standalone without needing prior search-flow session state.
"""
from __future__ import annotations

import atexit
import json
import re
import time
from datetime import datetime

import requests

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_BASE = "https://virtusa.taleo.net/careersection"
_SEARCH_URL = f"{_BASE}/rest/jobboard/searchjobs?lang=en&portal=101430233"
_DETAIL_URL = f"{_BASE}/ex/jobdetail.ftl"
_INDIA_LOCATION_ID = "200100250"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_SEARCH_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json",
    "Referer": f"{_BASE}/ex/jobsearch.ftl?lang=en&",
    "Origin": "https://virtusa.taleo.net",
    "X-Requested-With": "XMLHttpRequest",
    "TZ": "GMT+05:30",
    "tzname": "Asia/Kolkata",
}


class RateLimitError(Exception):
    """Raised on 429/500, persistent network failure, or a missing browser for description fetch."""


# ---------------------------------------------------------------------------
# Browser singleton — Firefox, reused across all description fetches
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


def _parse_date(raw: str) -> str:
    """Convert 'DD/MM/YYYY' -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _location_from_column(raw: str) -> str:
    """Parse a JSON-array-string like '["IN-KA-Bangalore"]' -> 'Bangalore, India'."""
    try:
        locs = json.loads(raw)
    except (ValueError, TypeError):
        return "India"
    if not locs:
        return "India"
    parts = locs[0].split("-")
    city = parts[-1].strip() if parts else ""
    return f"{city}, India" if city else "India"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 25,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    page_no = (start // 25) + 1

    body = {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": ""}, "valid": True},
        "filterSelectionParam": {"searchFilterSelections": [
            {"id": "POSTING_DATE", "selectedValues": []},
            {"id": "LOCATION", "selectedValues": [_INDIA_LOCATION_ID]},
            {"id": "JOB_FIELD", "selectedValues": []},
            {"id": "JOB_TYPE", "selectedValues": []},
            {"id": "JOB_SCHEDULE", "selectedValues": []},
            {"id": "JOB_LEVEL", "selectedValues": []},
        ]},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
            {"id": "LOCATION", "selectedValues": []},
            {"id": "JOB_FIELD", "selectedValues": []},
            {"id": "JOB_NUMBER", "selectedValues": []},
            {"id": "URGENT_JOB", "selectedValues": []},
            {"id": "EMPLOYEE_STATUS", "selectedValues": []},
            {"id": "STUDY_LEVEL", "selectedValues": []},
            {"id": "WILL_TRAVEL", "selectedValues": []},
            {"id": "JOB_SHIFT", "selectedValues": []},
        ]},
        "pageNo": page_no,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            s = requests.Session()
            s.get(f"{_BASE}/ex/jobsearch.ftl?lang=en&", headers={"User-Agent": _UA}, timeout=timeout)
            r = s.post(_SEARCH_URL, headers=_SEARCH_HEADERS, json=body, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Virtusa: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Virtusa fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Virtusa fetch: no response — {last_exc}")

    jobs: list[dict] = []
    for req in r.json().get("requisitionList", []):
        job_id = req.get("jobId", "")
        cols = req.get("column", [])
        title = (cols[0] if len(cols) > 0 else "").strip()
        if not (job_id and title):
            continue
        loc = _location_from_column(cols[1]) if len(cols) > 1 else "India"
        posting_date = _parse_date(cols[2]) if len(cols) > 2 else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": f"{_DETAIL_URL}?job={job_id}&lang=en",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 30,
) -> tuple[str, str]:
    """Fetch the full description via headless Firefox (JSF-rendered detail page)."""
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
        idx = body.find("Description")
        text = body[idx:] if idx != -1 else body
        return " ".join(text.split()), ""
    except Exception as exc:
        raise RateLimitError(f"Virtusa description fetch failed: {exc}") from exc
    finally:
        page.close()
        context.close()
