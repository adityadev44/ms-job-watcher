"""Southwest Airlines India job fetcher -- Phenom People, server-rendered JSON.

careers.southwestair.com is Phenom People. The India-scoped landing page at

    https://careers.southwestair.com/in/en/search-results

serves the full India job pool (21 total as of 2026-10-03) as a
`phApp.ddo` SSR JSON blob in the raw HTML -- same pattern as
cisco_fetcher.py and unitedairlines_fetcher.py. No Playwright needed.

Southwest Airlines opened a Hyderabad GCC (announced Sept 2026), and
all India hiring posts to this page. Confirmed via direct probing:
- `keywords=<term>` genuinely narrows server-side (21 total → 9 for
  "python"). `q=` is not supported; use `keywords=` only.
- Pagination is via `from=<offset>`; site page size is fixed at 10
  (requesting 50 still returns 10). from=10 returns jobs at offset 10.
- `sortBy=Most recent` is a working param (verified: returns most-recent
  postings first); used whenever `sort_by == "date"`.
- `location` field in job objects is already "City, Country, India"
  (e.g. "Hyderabad, Telangāna, India") -- no append-if-missing dance
  needed, and the /in/ URL path already scopes to India.

Full description text is NOT inline in the search response. `fetch_job_description`
hits the detail page (`/in/en/job/{reqId}/{slug}`) and reads the schema.org
`JobPosting` JSON-LD block. `datePosted` there is unreliable (same class of bug
as Cisco/United Airlines in PLAYBOOK.md); returns "" for date so matcher.py
keeps the accurate `posting_date` set in `fetch_jobs()`.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_BASE_URL = "https://careers.southwestair.com"
_SEARCH_URL = f"{_BASE_URL}/in/en/search-results"
_JOB_BASE = f"{_BASE_URL}/in/en/job"

_SITE_PAGE_SIZE = 10

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.experimentData", re.S)
_LDJSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _slugify(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-")
    return slug or "job"


def _get(url: str, params: dict | None, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"429 rate-limited fetching {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Southwest Airlines fetch failed: {exc}") from exc
    raise RateLimitError(f"Southwest Airlines fetch: no response -- {last_exc}")


def _fetch_ssr_page(
    keyword: str, offset: int, sort_by: str, timeout: int
) -> tuple[list[dict], int]:
    params: dict[str, str] = {"from": str(offset)}
    if keyword:
        params["keywords"] = keyword
    if sort_by == "date":
        params["sortBy"] = "Most recent"

    r = _get(_SEARCH_URL, params, timeout)

    m = _DDO_RE.search(r.text)
    if not m:
        return [], 0
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return [], 0

    block = data.get("eagerLoadRefineSearch", {}) or {}
    total_hits = block.get("totalHits", 0) or 0
    jobs = ((block.get("data") or {}).get("jobs")) or []
    return jobs, total_hits


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    collected: list[dict] = []
    offset = start
    end = start + num
    total_hits: int | None = None

    while offset < end:
        raw_jobs, reported_total = _fetch_ssr_page(keyword, offset, sort_by, timeout)
        if total_hits is None:
            total_hits = reported_total
        if not raw_jobs:
            break
        collected.extend(raw_jobs)
        offset += len(raw_jobs)
        if total_hits and offset >= total_hits:
            break
        if len(raw_jobs) < _SITE_PAGE_SIZE:
            break

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    for j in collected[: max(0, end - start)]:
        req_id = str(j.get("reqId") or "").strip()
        if not req_id or req_id in seen_ids:
            continue
        seen_ids.add(req_id)

        title = (j.get("title") or "").strip()
        if not title:
            continue

        loc = (j.get("location") or "").strip()
        if not loc:
            multi = j.get("multi_location") or []
            loc = multi[0] if multi else "India"

        posted = (j.get("postedDate") or "")[:10]

        jobs.append({
            "id": req_id,
            "title": title,
            "location": loc,
            "posting_date": posted,
            "application_url": f"{_JOB_BASE}/{req_id}/{_slugify(title)}",
        })
    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    r = _get(application_url, None, timeout)

    m = _LDJSON_RE.search(r.text)
    if not m:
        return "", ""
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return "", ""

    description = _strip_html(data.get("description") or "")
    # Deliberately NOT returning data.get("datePosted") -- same unreliable-
    # detail-page-date class as Cisco/United Airlines (see module docstring).
    return description, ""
