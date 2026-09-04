"""Fetches AstraZeneca India (Bengaluru/Chennai/Mumbai GCC) job listings from
careers.astrazeneca.com (TalentBrew).

ATS identification (Step 1, verified live 2026-09-03 from scratch — the
assigned brief flagged the prior research label as "plausible-unverified,
needs ATS confirmation", and the Wave 5/6 lesson is that a secondary-research
"custom" guess is wrong far more often than right). A plain `curl` of
`careers.astrazeneca.com` loads `tbcdn.talentbrew.com` CSS/JS assets under
`/company/7684/...` — the exact same vendor/CDN already confirmed for
Optum/Boeing/Disney (`optum_fetcher.py`, `boeing_fetcher.py`,
`disney_fetcher.py`). AstraZeneca's tenant org ID is 7684.

Confirmed via `careers.astrazeneca.com/sitemap.xml`, which advertises
canonical location-facet URLs: `/location/india-jobs/7684/1269750/2`
(country-level India facet; 1269750 = India, facetType 2 = country — the
same facet IDs TalentBrew reuses across every customer tenant in this repo,
Boeing/Disney included) plus narrower `/location/karnataka-india-jobs/...`,
`/location/bengaluru-karnataka-india-jobs/...`, `/location/chennai-tamil-nadu-
india-jobs/...`, and `/location/mumbai-maharashtra-india-jobs/...` facets. No
Hyderabad facet exists anywhere in the sitemap and none of the 34 live India
postings list a Hyderabad location — AstraZeneca's India GCC footprint today
is genuinely Bengaluru + Chennai + Mumbai only, not Hyderabad, confirmed by
checking the raw facet list rather than assuming the brief's "Bengaluru/
Hyderabad/Chennai" framing was accurate for every company in that batch.

Same "TalentBrew frontend" shape and HTML template as Disney (not Boeing's
older template): job cards use a plain `<h2>` for the title and
`<span class="job-location">` for location (no per-card date span on this
tenant at all, unlike Disney's `job-date-posted` — the posting date is only
available from the detail page's JSON-LD, same fallback already handled by
`matcher.py` for optum-style `(description, posting_date)` tuples). The
results container is `<section id="search-results-list">` (a `<section>`,
not Disney's `<ul id="search-results-jobs">`) nested inside the outer
`<section id="search-results" data-total-results="34" data-total-pages="3"
data-records-per-page="15" ...>`, which conveniently exposes total-page-count
as a data attribute instead of requiring the
`<span class="pagination-total-pages">` text-parsing Disney needed.

Key findings, both confirmed empirically against the live site 2026-09-03:
- The free-text `k=` query param on the India-facet URL is ignored
  server-side (`?k=engineer` returns the identical 34-job pool/count as no
  query string at all) — same "TalentBrew ignores keyword on the facet page"
  shape as Boeing/Disney. `/search-jobs?l=India` (the generic search page)
  also ignores the location filter, returning ~792 jobs (clearly the global
  pool, not India-scoped) — so the dedicated India-facet URL is the only
  reliable India-only surface, same as Boeing/Disney/PepsiCo/Deutsche Bank.
- Pagination is path-suffixed exactly like Disney's tenant:
  `{india_facet_url}/{page}` (1-indexed; page 1 is also the bare facet URL).
  15 jobs/page (`data-records-per-page="15"`), 34 total India jobs across 3
  pages (15/15/4) as of 2026-09-03.

Job-detail pages carry a clean schema.org `JobPosting` JSON-LD block, same
pattern as Boeing/Disney/Optum/SAP Labs/Schwab — full description +
`datePosted` ("YYYY-M-D", e.g. "2026-9-2") read from there, no browser
needed.

Live data check (2026-09-03): of the 34 current India postings, most are
Chennai (excluded by `exclude_locations`) or non-engineering Bengaluru/Mumbai
roles (statistical programming, regulatory affairs, SAP ABAP/FICO
consulting, forecasting analysts) that `title_family` correctly rejects.
Exactly one current posting is both a genuine software-engineering title
AND in a non-excluded location: "Software Engineering Lead" (Bengaluru,
job 92489082064). Its live JD is a real, explicit `AI / ML / Python` match —
names "LangChain", "AutoGen", "LlamaIndex" as orchestration frameworks,
"Pinecone"/"Weaviate"/"Milvus" as vector databases, and spells out "RAG
(Retrieval-Augmented Generation)" verbatim — not a broad-only false
positive. Several Chennai-only postings ("Lead Consultant - Snowflake and
Cortex AI Engineer", "Platform Engineer – Enterprise Golden Paths &
Automated Governance", "Director- Software Engineering Lead") look
tech-relevant too but never reach the skill check because Chennai is
excluded by location before Layer 3 runs — a real, current fact about where
this GCC's engineering work sits today, not a fetcher defect. No `.NET/C#`
matches exist in the current pool at all.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.astrazeneca.com"
_ORG_ID = "7684"
# Canonical India-country facet URL, taken verbatim from
# careers.astrazeneca.com's own sitemap.xml ("india-jobs" slug; facetTerm
# 1269750 = India, facetType 2 = country -- same facet IDs Boeing/Disney's
# TalentBrew tenants use).
_INDIA_URL = f"{_BASE_URL}/location/india-jobs/{_ORG_ID}/1269750/2"
_RECORDS_PER_PAGE = 15  # TalentBrew's page size on this tenant (verified)
_MAX_PAGES = 30  # safety cap in case pagination metadata is ever wrong

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
    """Raised when the site rate-limits after all retries are exhausted."""


def _normalize_ld_date(date_str: str) -> str:
    """Convert the JSON-LD 'YYYY-M-D' posting date to 'YYYY-MM-DD'."""
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) == 3:
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            pass
    return date_str


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_card(a_tag: Any) -> dict[str, str] | None:
    """Parse one TalentBrew job `<a data-job-id>` on this tenant's template."""
    job_id = (a_tag.get("data-job-id") or "").strip()
    if not job_id:
        return None

    href = a_tag.get("href", "")
    application_url = f"{_BASE_URL}{href}" if href.startswith("/") else href

    title_tag = a_tag.find("h2")
    title = title_tag.get_text(strip=True) if title_tag else ""
    if not title:
        return None

    loc_span = a_tag.find("span", class_="job-location")
    location = loc_span.get_text(strip=True) if loc_span else ""

    return {
        "id": job_id,
        "title": title,
        "location": location or "India",
        # No per-card date on this tenant (unlike Disney's job-date-posted) --
        # matcher.py overwrites this with the real value returned by
        # fetch_job_description's (description, posting_date) tuple.
        "posting_date": "",
        "application_url": application_url,
    }


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"AstraZeneca: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"AstraZeneca fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"AstraZeneca fetch failed for {url}: {last_exc}")


def _parse_page(html: str) -> tuple[list[dict[str, str]], int]:
    """Return (jobs on this page, total page count reported by the site)."""
    soup = BeautifulSoup(html, "html.parser")

    total_pages = 1
    results_section = soup.find(id="search-results")
    if results_section is not None:
        raw_total_pages = results_section.get("data-total-pages", "")
        if str(raw_total_pages).isdigit():
            total_pages = int(raw_total_pages)

    results_list = soup.find(id="search-results-list")
    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    if results_list is not None:
        for a_tag in results_list.find_all("a", attrs={"data-job-id": True}):
            card = _parse_card(a_tag)
            if card and card["id"] not in seen_ids:
                seen_ids.add(card["id"])
                jobs.append(card)

    return jobs, total_pages


def _fill_cache(timeout: int) -> None:
    r = _get(_INDIA_URL, timeout)
    jobs, total_pages = _parse_page(r.text)
    _cache.extend(jobs)

    page = 2
    while page <= min(total_pages, _MAX_PAGES):
        time.sleep(0.2)
        r = _get(f"{_INDIA_URL}/{page}", timeout)
        more_jobs, _ = _parse_page(r.text)
        if not more_jobs:
            break
        _cache.extend(more_jobs)
        page += 1


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of AstraZeneca India job listings.

    AstraZeneca's TalentBrew tenant ignores the keyword query param on the
    India-facet URL (confirmed empirically -- see module docstring), so every
    call returns the same India-only pool regardless of `keyword`/`location`;
    the shared matcher handles title/skill filtering afterward. The full pool
    is fetched (and paginated) once and cached in-module; `start`/`num` slice
    that cache so matcher.py's pagination loop terminates naturally once the
    cache is exhausted.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch an AstraZeneca job's full description + posting date.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block (same
    pattern as Boeing/Disney/Optum/SAP Labs/Schwab) -- no browser needed.
    """
    r = _get(application_url, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        raw_html = data.get("description", "") or ""
        description = _strip_html(raw_html) if raw_html else ""
        posting_date = _normalize_ld_date(data.get("datePosted", ""))
        return description, posting_date

    return "", ""
