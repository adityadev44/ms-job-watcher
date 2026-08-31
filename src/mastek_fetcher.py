"""Fetches Mastek (careers.mastek.com) India job listings via the SAP
SuccessFactors J2W HTML search API — the classic (non-Unify) J2W platform,
same family as Nomura/Capgemini/SAP Labs, but a different theme skin: view
source on /search/ shows real server-rendered `<li class="job-tile">` tiles
(not the `<tr class="data-row">` rows used by Capgemini/SAP Labs), each
carrying `data-url`, a `a.jobTitle-link` title, and `div[id*="-value"]`
fields for date/location/dept/business-unit. No CSRF/REST dance needed —
plain `requests` works, confirmed live 2026-08-31 (`j2w.init(...)` on the
page sets `ssoCompanyId: 'masteklimi'`, `ssoUrl: 'career44.sapsf.com'`).

`/search/` accepts plain GET with `q=<keyword>` and `locationsearch=india`,
paginated via `startrow=N` (12 results/page, `data-per-page="12"` — fixed,
not configurable). BUT: `q` is NOT safe to use for per-keyword fetching —
verified live that any keyword with zero literal substring hits (nonsense
strings, and even real terms like "dotnet" that don't appear verbatim in any
JD) silently falls back to an identical small default set instead of an
empty result (same failure class as TCS's "#" bug / Persistent's noisy
OR-match). A blank `q=` + `locationsearch=india`, by contrast, reliably
returns the genuine full India pool (39 jobs at last check) with stable,
non-overlapping pages. So this fetcher ignores the keyword entirely and
caches the whole India pool once per process, letting the shared matcher's
title/skill filters do the real narrowing (same pattern as CRED/Groww/
Razorpay/Persistent).

Pagination also wraps around: requesting `startrow` at or past the true
total silently drops the `locationsearch=india` filter and returns unrelated
jobs from the site's global default sort (observed: US/GB jobs bleeding in)
instead of an empty page — a new variant of the UBS/MUFG wraparound bug.
`_fill_cache` stops once the running count reaches the total parsed from the
first page's "Showing X to Y of Z Jobs" header, never issuing a request at
or past that offset.

Search-result locations are just country codes ("IN") or "City, IN" (never
the word "India") — normalised to "India"/"City, India" so matcher.py's
is_india_job() recognises them, same as Capgemini/SAP Labs.

Job-detail pages are plain server-rendered HTML: `span.jobdescription` for
the JD body, `meta[itemprop="datePosted"]` (format
"Fri Aug 28 02:00:00 UTC 2026") for the posting date — identical anchors to
Capgemini/Nomura/SAP Labs.

Titles are generic, level-banded IT-services bands ("Software Engineer",
"Senior Software Engineer", "Specialist II") covering wildly different
stacks under the same title — live samples included Java+AWS+Spring Boot+
AngularJS, Oracle Fusion/ERP, Salesforce, and a Data Analyst role alongside
a genuine SharePoint/.NET/C# posting. Title text carries no reliable signal
here, same class of problem as Wipro/HCLTech/DXC.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.mastek.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 12  # fixed by this J2W tile theme; not configurable via query params

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Mastek's J2W tenant."""


# Module-level cache: the search endpoint's per-keyword filtering is not
# trustworthy (see module docstring), so the full India pool is fetched once
# and fetch_jobs()/fetch_job_description() are served from it afterward.
_job_cache: list[dict] = []
_cache_filled: bool = False


# ---------------------------------------------------------------------------
# Date / location helpers
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert 'Aug 28, 2026' (search tile) to '2026-08-28'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Fri Aug 28 02:00:00 UTC 2026' (meta tag) to '2026-08-28'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_location(raw: str) -> str:
    """'IN' -> 'India'; 'Pune, IN' -> 'Pune, India'; other codes left as-is
    so matcher.py's is_india_job() correctly rejects non-India tiles."""
    loc_text = (raw or "").strip()
    if not loc_text:
        return "India"
    if loc_text.upper() == "IN":
        return "India"
    return re.sub(r",\s*IN\b", ", India", loc_text)


# ---------------------------------------------------------------------------
# Cache fill
# ---------------------------------------------------------------------------

def _fetch_page(start: int, timeout: int) -> str:
    """GET one /search/ page; 3-attempt retry with exponential backoff."""
    params: dict = {"q": "", "locationsearch": "india"}
    if start:
        params["startrow"] = start

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Mastek J2W: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Mastek search fetch failed: {exc}") from exc

    raise RateLimitError(f"Mastek search fetch: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire India job pool once; cache job list.

    _cache_filled is set before the request attempts so a transient failure
    doesn't trigger a retry storm on every fetch_jobs() call made during the
    same process run (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    total: int | None = None
    start = 0

    while True:
        html_text = _fetch_page(start, timeout)

        if total is None:
            m = re.search(r"Showing \d+ to \d+ of (\d+) Jobs", html_text)
            if not m:
                m = re.search(r"Showing (\d+) Job\b", html_text)
            if m:
                total = int(m.group(1))

        soup = BeautifulSoup(html_text, "html.parser")
        tiles = soup.select("li.job-tile")
        if not tiles:
            break

        for li in tiles:
            data_url = (li.get("data-url") or "").strip()
            job_id = data_url.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit() or job_id in seen_ids:
                continue

            link = li.select_one("a.jobTitle-link")
            title = html_mod.unescape(link.get_text(strip=True)) if link else ""
            if not title:
                continue

            loc_div = li.select_one('div[id*="-location-value"]')
            loc_text = _normalize_location(loc_div.get_text(strip=True) if loc_div else "")

            date_div = li.select_one('div[id*="-date-value"]')
            posting_date = (
                _parse_search_date(date_div.get_text(strip=True)) if date_div else ""
            )

            seen_ids.add(job_id)
            jobs.append({
                "id": job_id,
                "title": title,
                "location": loc_text,
                "posting_date": posting_date,
                "application_url": f"{_BASE_URL}{data_url}",
            })

        start += _PAGE_SIZE
        # Stop once the known total is covered, or after a generous safety
        # cap — never issue a request at/past the true total: startrow
        # beyond it silently drops locationsearch=india and returns
        # unrelated global-pool jobs instead of an empty page (see
        # module docstring).
        if total is not None and len(seen_ids) >= total:
            break
        if start > 1000:
            break
        time.sleep(0.2)

    _job_cache[:] = jobs
    print(f"[Mastek] Cache filled: {len(jobs)} India jobs")


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
    """Return a page of Mastek India jobs.

    Keyword/location are accepted but ignored — Mastek's own search silently
    falls back to a stale default set for keywords with no literal
    substring hit (see module docstring), so the full India pool is cached
    once and sliced here; the shared matcher does the real title/skill
    filtering.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Mastek job.

    Detail page uses <span class="jobdescription"> and
    <meta itemprop="datePosted" content="Fri Aug 28 02:00:00 UTC 2026">.
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Mastek description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Mastek description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Mastek description fetch: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")

    desc_span = soup.select_one("span.jobdescription")
    description = ""
    if desc_span:
        raw = html_mod.unescape(desc_span.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    date_meta = soup.find("meta", {"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_detail_date(date_meta.get("content", ""))

    return description, posting_date
