"""Fetches Qatar Airways job listings via careers.qatarairways.com (Avature).

ATS discovery (live, 2026-09-13): careers.qatarairways.com is an Avature
portal (``avacdn.net`` CSP references, ``avature.portal.*`` meta tags,
robots.txt pointing at ``qatarairways.avature.net/.../sitemap_index.xml``).
The portal's search UI (``/global/SearchJobs``) is a JS "search wizard" with
per-load-obfuscated internal field tokens that first looked like it would
need Playwright/a session dance (the same class of complexity as the SAP
SuccessFactors "Unify" theme) -- but that turned out to be a red herring.

The real, load-bearing discovery: the rendered page's own "Next Page" link
points at a **plain, unauthenticated, server-rendered** URL:

    https://careers.qatarairways.com/global/SearchJobs/?jobRecordsPerPage=6&jobOffset=<n>

A bare ``requests.get`` on that URL (no cookies, no session, no CSRF token)
returns the exact same HTML Playwright renders -- confirmed live by diffing
plain-HTTP output against a Playwright-rendered page. ``jobRecordsPerPage``
is accepted but ignored (always exactly 6 results per page, confirmed by
requesting 50 and still getting 6); ``jobOffset`` is the only page control
that matters. Total pool confirmed live at 189 global postings ("of 189
results" footer text), so ~32 pages need walking to cover every posting --
keyword/location query params are not exposed on this endpoint at all, so
(like Siemens/MetLife/BNP-style "cache the pool" fetchers) the whole board
is walked and India is filtered client-side.

Individual job-detail pages (``/global/JobDetail/{slug}/{id}``) are
ALSO plain server-rendered HTML -- no Playwright needed there either. Every
detail page carries a ``<div id="section1__content">`` block (heading
"Description") holding the full job description HTML, and a
``section0__content`` block (heading "General Information") with Ref #,
Location, Job family, Closing Date fields. The JSON-LD block on the same
page is deliberately NOT used for description (only carries title +
datePosted) but IS used as a supplementary posting-date fallback.

India presence: confirmed live via a Google/web search cross-check plus
direct scraping -- Qatar Airways runs a real, non-trivial IT development
centre in **Ahmedabad, India** ("Software Engineer - Java", "Senior
Software Engineer - Angular", "Software Test Engineer" postings all listed
with ``Work locations: India - Ahmedabad``). This is a genuine India tech
hub, unlike Emirates/Singapore Airlines (both investigated the same day and
found to have zero India-based IT postings -- see PLAYBOOK.md).

Location format on both the list-page card ("Work locations: India -
Ahmedabad") and the detail page ("Location: India-Ahmedabad") already
contains the literal substring "india" - no normalization needed for the
shared ``is_india_job()`` check.

Posting date: list-page card shows "Posting date: DD-MM-YYYY" (e.g.
"07-04-2026"); confirmed against the same job's JSON-LD ``datePosted``
("2026-04-08") that this is DD-MM-YYYY, not MM-DD-YYYY. Converted to
ISO YYYY-MM-DD.

Job ID: the numeric segment embedded in the JobDetail URL (e.g. ``27007``)
is used as the dedupe ID -- distinct from the internal "Ref #" shown in the
General Information block (e.g. "226754"), which is a separate requisition
number not exposed anywhere in the list page's own markup.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE = "https://careers.qatarairways.com"
_LIST_URL = f"{_BASE}/global/SearchJobs/"
_PAGE_SIZE = 6  # fixed server-side; jobRecordsPerPage is accepted but ignored

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_JOB_ID_RE = re.compile(r"/global/JobDetail/[^/]+/(\d+)/?$")
_DATE_RE = re.compile(r"Posting date:\s*(\d{2}-\d{2}-\d{4})")
_TOTAL_RE = re.compile(r"of\s*<b>\s*(\d+)\s*</b>\s*results", re.I)


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Qatar Airways: 429 rate-limited fetching {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Qatar Airways fetch failed: {exc}") from exc
    raise RateLimitError(f"Qatar Airways: no response — {last_exc}")


def _parse_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _extract_total(html: str) -> int | None:
    m = _TOTAL_RE.search(html)
    return int(m.group(1)) if m else None


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    for article in soup.select("article.article--result"):
        link = article.select_one("h3 a.link")
        if not link:
            continue
        href = (link.get("href") or "").strip()
        title = link.get_text(strip=True)
        m = _JOB_ID_RE.search(href)
        if not m or not title:
            continue
        job_id = m.group(1)

        loc = ""
        posted = ""
        for span in article.select(".article__content > span.paragraph"):
            text = span.get_text(" ", strip=True)
            if text.startswith("Work locations:"):
                loc = text.split(":", 1)[1].strip()
            elif text.startswith("Posting date:"):
                m2 = _DATE_RE.search(text)
                if m2:
                    posted = _parse_date(m2.group(1))

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posted,
            "application_url": href,
        })
    return jobs


# ---------------------------------------------------------------------------
# Whole-board cache — query params aren't exposed on this endpoint at all,
# so (like Siemens/MetLife) walk every page once per process and let the
# shared matcher filter India/title/skills client-side.
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_cache_filled = False
_cache_error: RateLimitError | None = None


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    offset = 0
    total: int | None = None
    max_pages = 80  # safety cap well above the ~32 pages a 189-job pool needs

    for _ in range(max_pages):
        url = f"{_LIST_URL}?jobRecordsPerPage={_PAGE_SIZE}&jobOffset={offset}"
        try:
            r = _get(url, timeout)
        except RateLimitError as exc:
            _cache_error = exc
            raise

        if total is None:
            total = _extract_total(r.text)

        page_jobs = _parse_page(r.text)
        if not page_jobs:
            break

        new_count = 0
        for j in page_jobs:
            if j["id"] in seen_ids:
                continue
            seen_ids.add(j["id"])
            collected.append(j)
            new_count += 1

        offset += _PAGE_SIZE
        if total is not None and offset >= total:
            break
        if new_count == 0:
            # Defensive: server started repeating a page instead of
            # returning fresh results or an empty tail (wraparound bug
            # class seen on MUFG/UBS/Nvidia) — stop instead of looping.
            break

    global _job_cache
    _job_cache = collected


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    _fill_cache(timeout=timeout)
    return _job_cache[start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date).

    Deliberately always returns "" for posting_date: the detail page's own
    JSON-LD ``datePosted`` field is unreliable on this tenant — confirmed
    live on job 27007 ("Software Engineer - Java - Ahmedabad, India"), whose
    JSON-LD says 2026-04-08 while the search-listing page's own "Posting
    date:" field (already captured in fetch_jobs()) correctly shows
    2026-09-13. Since matcher.py unconditionally overwrites
    job["posting_date"] with any non-empty date returned here, returning the
    JSON-LD value would silently corrupt the accurate date already set —
    same bug class as United Airlines/PolicyBazaar (see PLAYBOOK.md).
    """
    r = _get(application_url, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    desc_el = soup.select_one("#section1__content")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    return description, ""
