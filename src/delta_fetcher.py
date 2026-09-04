"""Fetches Delta Air Lines (Delta Technology Hub, Bengaluru) job listings
from dth.avature.net (Avature ATS).

Investigation history (assigned as a deliberately weak/thin-signal candidate
-- deferred from Wave 6 alongside Qualcomm/Novartis/AstraZeneca/Lufthansa/
AB InBev/DAZN):

- The prior surface-level finding ("Delta hires India SDEs via Talent500, an
  intermediary platform") was real but a dead end when actually verified.
  Talent500's frontend (`talent500.com/jobs/delta-airlines-india/...`) is a
  React SPA whose real backend is a genuine, stable, company-scoped REST API
  (`GET https://prod-warmachine.talent500.co/api/v3/jobs/search/
  ?company_slug=<slug>&size=N&offset=N` -- confirmed working end-to-end
  against other companies on the platform, e.g. `company_slug=t-mobile`
  returned 112 real jobs, and an unfiltered query returned 7,749 jobs
  platform-wide). Delta's own profile IS on Talent500
  (`GET /api/companies/` -> id `5c9824ea-3c75-4177-998c-348b68d7d21d`, slug
  `delta-airlines-india`, `publish: true`, `is_job_displayable: true`,
  `third_party_company: false`) -- but querying that slug (or its company_id)
  returns `{"total": 0, "data": []}` right now. This is a genuine current
  zero, not a broken query: the same mechanism returns real data for other
  companies, and even a giant employer already in this repo (Wells Fargo)
  also returns 0 via this same slug filter today -- Talent500 evidently has
  many company profiles currently sitting with no open reqs. The individual
  job URLs surfaced by web search/aggregators (T500-21663 etc.) are stale --
  their detail pages render the generic SPA shell with no job content,
  consistent with closed postings on a fast-turnover board (same shape as
  the Infosys "closed job, no error state" lesson in PLAYBOOK.md).

- Went back to Step 1 per the task brief and checked whether Delta has its
  own dedicated India careers presence instead. `www.delta.com/us/en/careers`
  is Akamai-fronted (redirects, bot-management JS, no usable content without
  a real browser) and is the *global* corporate site, not India-specific.
  `delta.eightfold.ai` exists and DOES serve a public (no-login) `/careers`
  landing page, but its `api/pcsx/search` endpoint returns HTTP 403
  `"PCSX is not enabled for this user"` for every `domain=` value tried --
  the exact same disabled-tenant shape already documented for HSBC in
  PLAYBOOK.md -- and unlike HSBC, no real/open job ID could be found to use
  as a "related jobs" widget anchor, so that workaround doesn't apply here.

- Found the real answer via a web search for "Delta Technology Hub" (the
  actual branded name of Delta's Bengaluru India engineering GCC):
  **deltatechhub.com**, Delta's own dedicated India careers microsite, links
  directly to `dth.avature.net/en_US/careers` -- a separate, real Avature
  tenant (same ATS family as `macquarie_fetcher.py`, a different Avature
  instance/template). This is a fully server-rendered HTML job board with
  **104 live India (Bengaluru-only) postings** confirmed at investigation
  time, including genuine `AI / ML / Python`-track material (e.g. "AI
  Software Engineer" job 27587's JD explicitly names "Large Language Models
  (LLMs)" and AI agent orchestration) and `.NET / C#`-track candidate titles
  ("Software Development Engineer" family, various seniorities). This is the
  real, stable, queryable India board this company needed -- Talent500 was a
  red herring for Delta specifically, not the actual answer.

ATS behavior (dth.avature.net):
- Search: GET `https://dth.avature.net/en_US/careers/SearchJobs/?jobOffset=N`
  -- plain server-rendered HTML, no JS/Playwright needed. Fixed page size of
  10; `jobOffset` increments by 10. A `?query=` free-text param exists but is
  genuinely ignored server-side (confirmed: `query=zzzznonexistentkeyword`
  and `query=Java` both return the identical "104 results" / job set as no
  query at all) -- same "ignores keywords" shape as Siemens/MetLife/Disney.
  Pagination terminates cleanly: a page past the true total returns an empty
  `<ul class="list list--jobs">` with zero `<li>` items (verified at
  `jobOffset=110` against a 104-job total) -- no wraparound-past-total bug
  like UBS/MUFG/Nvidia/Pfizer/Walmart, so a simple "stop on empty page" loop
  is sufficient.
- Every single posting on this tenant is "India, Bangalore." (verified across
  the full paginated result set, not just a sample) -- this is Delta's
  dedicated India GCC portal, so there is no non-India leakage to filter
  the way Micron/Verizon/Lowe's needed. Location text is reordered from the
  site's own "Country, City." order to the repo's usual "City, India" shape.
- Job-detail pages (`JobDetail/{slug}/{id}?jobId={id}`) carry a clean
  schema.org `JobPosting` JSON-LD block with a rich HTML `description` and
  an already-zero-padded `datePosted` ("YYYY-MM-DD") -- same pattern as
  Disney/Schwab/Boeing/SAP Labs, no browser needed. `jobLocation` inside the
  JSON-LD is empty/unpopulated on this tenant, so location is taken from the
  search-listing page instead (always available and always "India,
  Bangalore." anyway).
- Because keyword is ignored server-side and the pool is small (~104 jobs,
  11 pages), the full pool is fetched once and cached in-module -- same
  "ignores keywords, cache once" pattern as Disney/Siemens/MetLife -- so
  matcher.py's repeated per-keyword calls don't re-walk all 11 pages every
  time.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://dth.avature.net"
_SEARCH_URL = f"{_BASE_URL}/en_US/careers/SearchJobs/"
_PAGE_SIZE = 10  # fixed by the site
_MAX_PAGES = 60  # defensive cap (~600 raw jobs) against runaway pagination

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_cache: list[dict[str, str]] = []
_cache_filled = False


class RateLimitError(Exception):
    """Raised when the site rate-limits (429) or persistently fails."""


def _normalize_location(raw: str) -> str:
    """Reorder Avature's "India, Bangalore." to the repo's "Bangalore, India"."""
    text = (raw or "").strip().rstrip(".").strip()
    if not text:
        return "India"
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) >= 2 and parts[0].lower() == "india":
        city = ", ".join(parts[1:])
        return f"{city}, India"
    if "india" not in text.lower():
        return f"{text}, India"
    return text


def _normalize_ld_date(date_str: str) -> str:
    """Normalize the JSON-LD posting date to 'YYYY-MM-DD' (already zero-padded
    on this tenant, but defend against a non-padded 'YYYY-M-D' variant too,
    same as the Disney/Schwab fetchers on other Avature/TalentBrew tenants).
    """
    if not date_str:
        return ""
    parts = date_str.strip().split("-")
    if len(parts) == 3:
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            pass
    return date_str


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Delta: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Delta fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Delta fetch failed for {url}: {last_exc}")


def _parse_search_page(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for li in soup.select("li.list__item"):
        link = li.select_one(".list__item__text__title a")
        if not link:
            continue
        href = (link.get("href") or "").strip()
        title = link.get_text(strip=True)
        if not (href and title):
            continue

        m = re.search(r"jobId=(\d+)", href)
        if not m:
            continue
        job_id = m.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        spans = li.select(".list__item__text__subtitle span")
        loc_text = spans[0].get_text(strip=True) if spans else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": _normalize_location(loc_text),
            "posting_date": "",  # filled in via JSON-LD on description fetch
            "application_url": href,
        })

    return jobs


def _fill_cache(timeout: int) -> None:
    offset = 0
    seen_ids: set[str] = set()
    for _ in range(_MAX_PAGES):
        url = f"{_SEARCH_URL}?jobOffset={offset}"
        r = _get(url, timeout)
        page_jobs = _parse_search_page(r.text)
        if not page_jobs:
            break

        new_count = 0
        for job in page_jobs:
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            new_count += 1
            _cache.append(job)

        if new_count == 0:
            break  # stalled -- stop rather than loop forever

        offset += _PAGE_SIZE
        time.sleep(0.15)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Delta Technology Hub (Bengaluru) India job listings.

    This tenant ignores `keyword`/`location` query params server-side
    (confirmed empirically -- see module docstring), so every call returns
    the same India-only pool regardless of the caller's arguments; the
    shared matcher handles title/skill filtering afterward. The full pool is
    fetched (and paginated) once and cached in-module; `start`/`num` slice
    that cache so matcher.py's pagination loop terminates naturally once the
    cache is exhausted.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a Delta Technology Hub job's full description + posting date.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block (same
    pattern as Disney/Schwab/Boeing/SAP Labs) -- no browser needed.
    """
    r = _get(application_url, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data: Any = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        raw_html = data.get("description", "") or ""
        description = _strip_html(raw_html) if raw_html else ""
        posting_date = _normalize_ld_date(data.get("datePosted", "") or "")
        return description, posting_date

    return "", ""
