"""Cisco India job fetcher -- Phenom People, server-rendered JSON.

careers.cisco.com is Phenom People (same CDN pattern as Morningstar/United
Airlines -- `cdn.phenompeople.com/CareerConnectResources/...` assets,
`phApp.ddo` SSR JSON blob). Confirmed via direct DevTools-style probing
2026-09-03, NOT the "custom" ATS label prior secondary research guessed
(same wrong-guess pattern as Boeing/PepsiCo/Walmart/United Airlines in
PLAYBOOK.md's Wave 5 entry -- verified from scratch here rather than
trusted).

The assigned URL `careers.cisco.com/global/en/india/etr-jobs` is a small
curated landing page (only 13 total jobs, "ETR" = Cisco's Engineering,
Technology & Research org's own recruiting microsite) -- NOT Cisco's full
India job board. The real, much larger India-scoped search page is:

    https://careers.cisco.com/global/en/india/search-results

A plain unauthenticated `requests.get` on that URL already embeds the full
search response as a JSON blob assigned to `phApp.ddo` in the raw HTML (same
SSR-JSON shape as this repo's Google/United Airlines fetchers) -- no
Playwright needed anywhere in this pipeline. Confirmed live: this page's
totalHits (275, unfiltered) exactly matches the "country" facet's own
India count (275) taken from Cisco's *global* search-results page
(`careers.cisco.com/global/en/search-results`, no params) -- i.e. this is
genuinely the complete India pool, not a subset.

Confirmed via direct probing (not assumed):
- `keywords=<term>` genuinely narrows server-side for real terms (233 for
  "software", 25 for "developer", 26 for ".NET developer", 25 for "C#
  developer", 19 for "generative", 196 for "machine learning engineer").
  BUT bare "AI" as a token is effectively a no-op here -- "AI" alone and
  "AI engineer" both return totalHits 275, identical to no keyword filter
  at all, while "generative" alone (19) legitimately narrows. Since this
  repo's shared `default_keywords` includes both "AI engineer" and
  "generative ai engineer", those two keyword passes will fetch (and page
  through) the *entire* India pool here rather than a narrowed subset --
  not incorrect (dedup handles it, and it can't cause a false negative),
  just worth knowing this is why those two passes are slow/broad. Distinct
  from TCS's "#" bug (that one broke the query outright); this is closer to
  a short/common-token-as-stopword behavior on Cisco's search index.
- Pagination is via `from=<offset>` (confirmed combining correctly with
  `keywords=`); the site's own page size is fixed at 10 regardless of any
  `num=`/`hitsPerPage=` override (tested, ignored). Unlike the MUFG/UBS/
  Nvidia/Pfizer/Walmart wraparound-past-total bug, Cisco's `from=` cleanly
  returns an empty `jobs` list (hits 0, `totalHits` still correctly
  reported) once offset >= total -- verified at both from=275 (exact
  boundary) and from=300 (past it). No page-1-first-ID memo needed here.
- `sortBy=Most recent` is a genuine working param (verified: default order
  mixes older postings first; passing `sortBy=Most recent` returns jobs
  posted "today" first) -- used here whenever `sort_by == "date"`.
- Per-job `country` field is present and reliable ("India" exact string) --
  used as a defensive filter, because the /india/-scoped search page still
  occasionally leaks a non-India result for a broad keyword (e.g. a
  "Leader, Solutions Engineer | Minato, Tokyo, Japan" posting surfaced under
  the "software engineer" keyword search) -- same facet-leakage class as
  Micron/Verizon/Lowe's/Walmart's "AI engineer"->PepsiCo entries in
  PLAYBOOK.md. matcher.py's own is_india_job() would also reject that job
  (its `location` field carries no "india" substring), so this is a belt-
  and-suspenders filter using a strictly more precise per-job field.
- The `location` field returned by the search API is already a clean
  "City, India" string (no ", India" append-if-missing dance needed, unlike
  most Workday tenants in this repo).

Full description text is NOT inline in the search response (`descriptionTeaser`
there is a ~250-char marketing blurb, not the real JD) -- `fetch_job_description`
hits the job's own detail page (`/global/en/job/{reqId}/{any-slug}` --
confirmed the server resolves purely by the `{reqId}` path segment, same as
United Airlines/TCS/Infosys; a deliberately wrong slug still 200s with the
correct job) and reads the single schema.org `JobPosting` JSON-LD block
(confirmed only one `application/ld+json` block per page here -- NOT the
Wipro/HCLTech double-`itemprop="description"` trap). Its `description` field
is HTML-entity-encoded HTML (`&lt;p&gt;...`) -- unescape once, then strip tags.

Bug avoided (same class as United Airlines' entry in PLAYBOOK.md's Key Bugs
table): the JSON-LD detail page's own `datePosted` is NOT the real posting
date -- sampled two jobs whose search-response `postedDate` was 2026-08-25
and 2026-05-22 respectively; their detail-page JSON-LD `datePosted` read
2026-08-26 and 2026-08-15 (drifted toward "recently", not accurate).
`fetch_job_description` deliberately returns "" for the date so matcher.py
keeps the accurate `posting_date` already set in `fetch_jobs()`.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_BASE_URL = "https://careers.cisco.com"
_SEARCH_URL = f"{_BASE_URL}/global/en/india/search-results"
_JOB_BASE = f"{_BASE_URL}/global/en/job"

_SITE_PAGE_SIZE = 10  # fixed by the ATS; every num/hitsPerPage override is ignored

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
    """Raised on 429 / persistent connection failure from Cisco's careers site."""


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
            raise RateLimitError(f"Cisco fetch failed: {exc}") from exc
    raise RateLimitError(f"Cisco fetch: no response -- {last_exc}")


def _fetch_ssr_page(
    keyword: str, offset: int, sort_by: str, timeout: int
) -> tuple[list[dict], int]:
    """One raw request to the India search-results page at a given offset.

    Returns (jobs_on_this_page, total_hits_reported_by_the_api).
    """
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
    # The site's own page size is fixed at 10 regardless of query params, but
    # matcher.py calls this with num=20 -- accumulate as many fixed-size raw
    # pages as needed to satisfy [start, start + num).
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
        # Defensive India check -- the /india/-scoped page occasionally
        # leaks a non-India result for a broad keyword (see module
        # docstring). Skip anything whose own `country` field isn't India.
        country = (j.get("country") or "").strip().lower()
        if country and country != "india":
            continue

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
    # Deliberately NOT returning data.get("datePosted") here -- confirmed
    # unreliable (drifts toward "recently", not the real posting date). See
    # module docstring. Returning "" leaves fetch_jobs()'s accurate date
    # untouched (matcher.py only overwrites posting_date with a non-empty
    # value).
    return description, ""
