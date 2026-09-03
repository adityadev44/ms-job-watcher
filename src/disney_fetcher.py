"""Fetches Disney (Bengaluru/Mumbai India) job listings from disneycareers.com
(TalentBrew).

ATS identification (Step 1, verified live 2026-09-03 from scratch — the
assigned brief flagged the prior "custom" label as unverified secondary
research, per the Wave 5 lesson that 4 of 7 such guesses were wrong).
`disneycareers.com` (redirects to `www.disneycareers.com`) loads
`tbcdn.talentbrew.com` script bundles (`headutil.js`, `plumrnizr-a.js`,
`jquery-client.js`, `tb-corepack.js`, company asset path `/company/391/...`)
— the same vendor/CDN already confirmed for Optum and Boeing
(`boeing_fetcher.py`). Disney's tenant org ID is 391.

Same "TalentBrew frontend" shape as Boeing, but a different HTML template on
this tenant: job cards use a plain `<h2>` for the title (not a
`search-results__job-title` span), `<span class="job-location">` /
`<span class="job-date-posted">` for location/date, and the results `<ul
id="search-results-jobs" data-results-count="N">` carries the total count
directly (no `<section data-total-results>` wrapper like Boeing's). Verified
via `disneycareers.com`'s own `sitemap.xml`, which advertises canonical
location-facet URLs: `/en/location/india-jobs/391/1269750/2` (country-level
India facet; 1269750 = India, facetType 2 = country, same facet IDs Boeing's
tenant uses — TalentBrew shares these across customers) and a narrower
`/en/employment/bengaluru-karnataka-india-technology-jobs/391/...` combined
facet.

Key findings, both confirmed empirically against the live site 2026-09-03:
- The free-text `k=`/`l=` query params on `/en/search-jobs` and even on the
  India-facet URL itself are BOTH ignored server-side — `?k=engineer` on the
  facet page returns the identical 27-job pool as no query string at all, and
  `/en/search-jobs?k=engineer&l=India` returns non-India locations (Florida,
  Hong Kong, Sweden, UK) mixed into the "India" results, proving `l=India` is
  not a real filter either (same shape as Boeing's ignored `?l=India`, unlike
  Optum's tenant where it works). The India-facet URL is the only reliable
  India-only surface; keyword narrowing is left entirely to the shared
  matcher, same as Boeing/Deutsche Bank/UBS/PepsiCo.
- Unlike Boeing (where real pagination was never reverse-engineered and the
  fetcher warns instead of missing pages), Disney's tenant paginates cleanly
  via a path suffix: `{india_facet_url}/{page}` (1-indexed; page 1 is also
  the bare facet URL). `<span class="pagination-total-pages">of N</span>` on
  every page (including page 1) gives the true page count; 10 jobs/page,
  currently 27 total India jobs across 3 pages (10/10/7).

Job-detail pages carry a clean schema.org `JobPosting` JSON-LD block, same
pattern as Boeing/Optum/SAP Labs/Schwab — full description + `datePosted`
("YYYY-M-D") read from there, no browser needed.

Live data check (2026-09-03): all 27 current India postings are Bengaluru or
Mumbai (no Chennai/Pune/Tamil Nadu/Kochi) — mostly Industrial Light & Magic
(VFX/animation: Modeler, Motion Editor, Look Dev TD — excluded by
`title_family` not matching those titles) and Disney Experiences/Corporate
roles. A handful of genuine "Software Engineer"/"Platform Engineer"/
"Security Engineer" titles pass Layer 2, but their live descriptions mention
only broad-only terms (Azure/React) — zero `primary_skills` hard hits on
either track today, so 0 real matches currently exist. See PLAYBOOK.md's
"Key Bugs" table for a title-abbreviation gap this uncovered (not fixed
here, per the "flag don't silently patch" convention): this tenant
abbreviates "Manager"/"Director" as "Mgr"/"Dir" in titles (e.g. "Dir,
Software Engineering", "Mgr, Software Engineer") — `exclude_terms`'s
word-boundary match on the literal words "manager"/"director" does not catch
these abbreviated forms, so a future Disney JD with these titles that
happens to name a hard .NET/AI skill would incorrectly pass Layer 2 as if it
were an IC role.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://www.disneycareers.com"
_ORG_ID = "391"
# Canonical India-country facet URL, taken verbatim from disneycareers.com's
# own sitemap.xml ("india-jobs" slug; facetTerm 1269750 = India, facetType 2
# = country — same facet IDs as Boeing's TalentBrew tenant).
_INDIA_URL = f"{_BASE_URL}/en/location/india-jobs/{_ORG_ID}/1269750/2"
_RECORDS_PER_PAGE = 10  # TalentBrew's page size on this tenant (verified)
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

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_cache: list[dict[str, str]] = []
_cache_filled = False


class RateLimitError(Exception):
    """Raised when the site rate-limits after all retries are exhausted."""


def _normalize_card_date(date_str: str) -> str:
    """Convert TalentBrew's search-card date ('Sept. 02, 2026') to 'YYYY-MM-DD'."""
    m = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),\s+(\d{4})$", date_str.strip())
    if not m:
        return ""
    mon_raw, day, year = m.groups()
    month = _MONTHS.get(mon_raw.strip().lower())
    if not month:
        return ""
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


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

    date_span = a_tag.find("span", class_="job-date-posted")
    posting_date = _normalize_card_date(date_span.get_text(strip=True)) if date_span else ""

    return {
        "id": job_id,
        "title": title,
        "location": location or "India",
        "posting_date": posting_date,
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
                raise RateLimitError(f"Disney: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Disney fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Disney fetch failed for {url}: {last_exc}")


def _parse_page(html: str) -> tuple[list[dict[str, str]], int]:
    """Return (jobs on this page, total page count reported by the site)."""
    soup = BeautifulSoup(html, "html.parser")

    total_pages = 1
    pages_span = soup.find("span", class_="pagination-total-pages")
    if pages_span:
        m = re.search(r"(\d+)", pages_span.get_text())
        if m:
            total_pages = int(m.group(1))

    results_ul = soup.find("ul", id="search-results-jobs")
    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    if results_ul is not None:
        for a_tag in results_ul.find_all("a", attrs={"data-job-id": True}):
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
    """Return a page of Disney India job listings.

    Disney's TalentBrew tenant ignores keyword/location query params on the
    India-facet URL (confirmed empirically — see module docstring), so every
    call returns the same India-only pool regardless of `keyword`/`location`;
    the shared matcher handles title/skill filtering afterward. The full
    pool is fetched (and paginated) once and cached in-module; `start`/`num`
    slice that cache so matcher.py's pagination loop terminates naturally
    once the cache is exhausted.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a Disney job's full description + posting date.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block (same
    pattern as Boeing/Optum/SAP Labs/Schwab) — no browser needed.
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
