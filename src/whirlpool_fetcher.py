"""Fetches Whirlpool Corporation job listings via the Eightfold PCSX search API.

ATS discovery (live, 2026-09-14): ``jobs.whirlpool.com``/
``www.whirlpoolcareers.com`` redirect to an Eightfold-hosted careers site
(``whirlpool.eightfold.ai``, confirmed via the page's own
``eightfold-font-base.css``/`window.COUNTRY_CODE` markers) -- the same ATS
and public search API shape as this repo's Microsoft fetcher
(``/api/pcsx/search``, no PCSX-disabled 403 the way HSBC/Deere hit).
Plain unauthenticated ``requests`` works for both search and detail.

``location=India`` genuinely narrows server-side (confirmed: 25 India
postings vs. a much larger global pool); ``q=``/keyword is a no-op here --
confirmed live across several distinct keywords (blank, "software
engineer", ".NET developer", "python developer" all return the identical
``count: 25``) -- so this fetcher caches the small India pool once and
lets matcher.py do the real per-keyword narrowing, same reasoning as
Continental/MBRDI/Ford.

India presence confirmed NOT purely an excluded city: of 25 postings, 16
are Pune (Whirlpool's India engineering/R&D hub, excluded) and 2 are
Kolhapur (a plant town, not excluded but not software), but genuine
non-Pune/non-excluded postings also exist -- Gurugram ("Salesforce
Developer", "Senior Manager, PMO", "Deputy Manager, Quality Generalist",
"Sr Executive - FPS"), Mumbai, Bangalore, and Hyderabad ("Branch
Commercial Manager" x2). Current live snapshot has 0 matches: the
software/hardware engineering titles are all Pune-based (excluded), and
the one genuine non-Pune tech-adjacent role ("Salesforce Developer",
Gurugram) doesn't name a tracked primary skill -- included anyway per the
Ford/Volvo Cars/Allstate precedent (a real, queryable, working board with
a genuine multi-city India footprint, not a structural Pune-only zero).

Locations arrive as clean "City,State,IND" triples (e.g.
"Pune,Maharashtra,IND") -- converted to "City, India" so matcher.py's
exclude_locations city/state-name matching still works.
"""

from __future__ import annotations

import time
import warnings
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

_DOMAIN = "whirlpool.com"
_SEARCH_URL = "https://whirlpool.eightfold.ai/api/pcsx/search"
_DETAIL_URL = "https://whirlpool.eightfold.ai/api/apply/v2/jobs/{job_id}"
_JOBS_BASE = "https://jobs.whirlpool.com/careers/job"
_PAGE_SIZE = 10  # this tenant hard-caps num/start pages at 10 regardless of `num`

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Module-level cache -- filled once per process, reused for every keyword pass
# (this fetcher ignores `keyword`; see module docstring).
_india_cache: list[dict] = []
_cache_filled = False


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from the Eightfold PCSX API."""


def _location_from(job: dict) -> str:
    locs = job.get("locations") or []
    if not locs:
        return "India"
    raw = locs[0]
    city = raw.split(",")[0].strip()
    return f"{city}, India" if city else "India"


def _fill_cache(timeout: int = 20) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before the loop -- avoid retry storms

    collected: list[dict] = []
    start = 0
    for _ in range(20):  # safety cap (~200 jobs), well beyond the known ~25-job pool
        for attempt in range(3):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = requests.get(
                        _SEARCH_URL,
                        params={"domain": _DOMAIN, "location": "India", "num": _PAGE_SIZE, "start": start},
                        headers=_HEADERS,
                        timeout=timeout,
                    )
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Whirlpool: 429 rate-limited")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Whirlpool cache fill failed: {exc}") from exc

        positions = r.json().get("data", {}).get("positions") or []
        if not positions:
            break

        for p in positions:
            job_id = str(p.get("id") or "")
            title = (p.get("name") or "").strip()
            if not job_id or not title:
                continue
            posted_ts = p.get("postedTs")
            posting_date = (
                datetime.fromtimestamp(posted_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                if posted_ts else ""
            )
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from(p),
                "posting_date": posting_date,
                "application_url": f"{_JOBS_BASE}/{job_id}",
            })

        start += _PAGE_SIZE
        time.sleep(0.2)

    _india_cache = collected
    print(f"[Whirlpool] Cache filled: {len(_india_cache)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Whirlpool India job listings.

    Keyword is ignored -- the whole (small, ~25-job) India pool is cached
    once via the server-side `location=India` filter and served in
    slices; matcher.py's title/skill filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) via the PCSX job detail API."""
    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    _DETAIL_URL.format(job_id=job_id),
                    params={"domain": _DOMAIN},
                    headers=_HEADERS,
                    timeout=timeout,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Whirlpool description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Whirlpool description fetch failed: {exc}") from exc

    data = r.json()
    raw_html = data.get("job_description", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    return description, ""
