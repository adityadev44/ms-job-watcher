"""
United Airlines job fetcher — India Knowledge Center (IKC), Phenom People.

careers.united.com is Phenom People (same CDN pattern as Morningstar). The
generic `/widgets` search API this platform normally exposes is unusable from
plain HTTP here too (confirmed: it 303-redirects / returns the bare HTML
shell without a session, same as the Morningstar lesson) — but United
publishes a dedicated, India-only landing page for its Gurugram/Bengaluru
subsidiary at:

    https://careers.united.com/us/en/india-knowledge-center-job-search

A plain unauthenticated `requests.get` on that page (no Playwright, no JS)
already embeds the *entire* search response as a JSON blob assigned to
`phApp.ddo` in the raw server-rendered HTML — the same "SSR JSON" shape as
this repo's Google fetcher, just under a different variable name. No
Playwright/browser session is needed anywhere in this pipeline.

Confirmed via direct probing (not assumed):
- `keywords=<term>` genuinely filters server-side (totalHits 32 -> 12 for
  "engineer" -> 0 for a nonsense keyword). `q=` is a silent no-op — don't use it.
- Pagination is via `from=<offset>` (NOT `start=`/`num=`/`hitsPerPage=`, all of
  which are silently ignored); the site's own page size is fixed at 10
  regardless of any query param, confirmed by requesting num=50/hitsPerPage=50
  and getting "hits":10 back every time.
- Every job on this page is genuinely India (Gurugram, Haryana or Bengaluru,
  Karnataka) across all pages sampled — no facet leakage to audit around here.
- A separate generic `careers.united.com/us/en/search-results?country=India`
  endpoint exists and *looks* more authoritative (totalHits 186 vs this page's
  32), but its `country` facet is badly broken — sampled results came back as
  Chicago/Illinois (WHQ-prefixed IDs, "World Headquarters") and Houston/Texas
  (IAH-prefixed, an airport code), not India at all. Deliberately NOT used —
  same class of bug as the Micron/Verizon/Lowe's facet-leakage entry in
  PLAYBOOK.md. The dedicated IKC landing page above is the reliable source.

Full description text is NOT inline in the search response — `ml_job_parser`
there only carries a ~200-char boilerplate teaser. `fetch_job_description`
hits the job's own detail page (`/us/en/job/{reqId}/{any-slug}` — confirmed
the server resolves purely by the `{reqId}` path segment; a deliberately wrong
slug still returns HTTP 200 with the correct job) and reads the schema.org
`JobPosting` JSON-LD block, same pattern as this repo's SAP Labs/Schwab/
Societe Generale fetchers. Its `description` field is single-level
HTML-entity-encoded HTML (`&lt;br&gt;...`) — unescape once, then strip tags.

Bug avoided (worth flagging in PLAYBOOK.md's Key Bugs table): the JSON-LD
detail page's own `datePosted` field is NOT the real posting date — sampling
several jobs, it consistently sits within a day of *today* regardless of the
job's actual posting date (a real ~2026-08-25 posting still reads
`datePosted: 2026-09-03`), while the search response's own `postedDate` field
correctly reflects the true, distinct posting date per job. Since
`matcher.py` unconditionally overwrites `job["posting_date"]` with whatever
non-empty date `fetch_job_description` returns, this fetcher deliberately
returns `""` for the date there instead of the misleading `datePosted` value
— keeping the accurate date already set in `fetch_jobs()`. (Same bug class as
PolicyBazaar's RSS `pubDate` always being a template default, not a real
date — see PLAYBOOK.md.)
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_BASE_URL = "https://careers.united.com"
_SEARCH_URL = f"{_BASE_URL}/us/en/india-knowledge-center-job-search"
_JOB_BASE = f"{_BASE_URL}/us/en/job"

_SITE_PAGE_SIZE = 10  # fixed by the ATS; every num/hitsPerPage override is ignored

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.experimentData", re.S)
_LDJSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


class RateLimitError(Exception):
    pass


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
            raise RateLimitError(f"United Airlines fetch failed: {exc}") from exc
    raise RateLimitError(f"United Airlines fetch: no response — {last_exc}")


def _fetch_ssr_page(keyword: str, offset: int, timeout: int) -> tuple[list[dict], int]:
    """One raw request to the IKC landing page at a given item offset.

    Returns (jobs_on_this_page, total_hits_reported_by_the_api).
    """
    params = {"from": offset}
    if keyword:
        params["keywords"] = keyword

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
    # The site's own page size is fixed at 10 regardless of query params, but
    # matcher.py calls this with num=20 — accumulate as many fixed-size raw
    # pages as needed to satisfy [start, start + num).
    collected: list[dict] = []
    offset = start
    end = start + num
    total_hits: int | None = None

    while offset < end:
        raw_jobs, reported_total = _fetch_ssr_page(keyword, offset, timeout)
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
        req_id = j.get("reqId") or j.get("jobId") or ""
        if not req_id or req_id in seen_ids:
            continue
        seen_ids.add(req_id)
        title = (j.get("title") or "").strip()
        loc = j.get("location") or j.get("cityStateCountry") or ""
        posted = (j.get("postedDate") or j.get("dateCreated") or "")[:10]
        jobs.append({
            "id": str(req_id),
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
    # Deliberately NOT returning data.get("datePosted") here — confirmed
    # unreliable (tracks "today", not the real posting date). See module
    # docstring. Returning "" leaves fetch_jobs()'s accurate date untouched.
    return description, ""
