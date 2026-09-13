"""Fetches Comcast (India) job listings from jobs.comcast.com (TalentBrew).

ATS identification (Step 1, verified live 2026-09-13): `jobs.comcast.com`
loads `tbcdn.talentbrew.com` assets under company org id `45483` — the same
TalentBrew vendor already confirmed for Optum/Boeing/Disney/AstraZeneca
elsewhere in this repo (`disney_fetcher.py` is the closest existing
reference; this fetcher follows the same overall shape). Comcast's own
sitemap.xml advertises the canonical India country-facet URL directly:
`/location/india-jobs/45483/1269750/2` (facet term 1269750 = India, same
cross-customer TalentBrew facet ID Disney/Boeing/AstraZeneca all use).

Unlike Disney's tenant, this template reports the result count via
`<span class="search-results__results-count">N</span>` inside an `<h1>`
rather than a `data-results-count` attribute on the results `<ul>`, and the
job cards live inside `<section id="search-results-list">` (no `id` on the
`<ul>` itself). There is also no `pagination-total-pages` element on this
tenant when the whole pool fits on one page — pagination is instead driven
purely by walking the `/{page}` path suffix until a page comes back with no
job cards (confirmed live: page 2 of the India facet returns the same
`results-count` label but zero job cards).

Key finding, confirmed live 2026-09-13 — **most of Comcast's India
engineering hiring is Chennai, not Bengaluru/Hyderabad**: of the 14 current
India postings, 13 are explicitly "Chennai, Tamil Nadu" (Comcast's India
Engineering Center) — an excluded location per this repo's `exclude_locations`
policy — and only 1 ("Network Test Engineer 4", multi-location Karnataka +
Remote) sits in a non-excluded location, and even that one title doesn't
match `title_family`. This pipeline is still wired up (the ATS is
trivially confirmed working, genuinely India-scoped, and non-Chennai
postings do appear on this tenant from time to time per the one live
example found), matching the precedent set by `disney_fetcher.py` (added
despite 0 live matches at the time) — but do not be surprised if this
company alerts rarely given how Chennai-heavy its current pool is. If a
future audit finds this pipeline has produced zero matches over a long
window purely because of the Chennai concentration, that would be a
legitimate case to reconsider, not a fetcher bug.

Job-detail pages carry a clean schema.org `JobPosting` JSON-LD block, same
pattern as Disney/Boeing/Optum — full description + `datePosted`
("YYYY-M-D") read from there, no browser needed.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.comcast.com"
_ORG_ID = "45483"
# Canonical India-country facet URL, taken verbatim from jobs.comcast.com's
# own sitemap.xml ("india-jobs" slug; facetTerm 1269750 = India, facetType 2
# = country — same facet IDs as Disney's/Boeing's TalentBrew tenants).
_INDIA_URL = f"{_BASE_URL}/location/india-jobs/{_ORG_ID}/1269750/2"
_MAX_PAGES = 30  # safety cap in case pagination ever misbehaves

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
                raise RateLimitError(f"Comcast: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Comcast fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Comcast fetch failed for {url}: {last_exc}")


def _parse_page(html: str) -> list[dict[str, str]]:
    """Return the job cards found on one results page."""
    soup = BeautifulSoup(html, "html.parser")

    results_section = soup.find("section", id="search-results-list")
    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    if results_section is not None:
        for a_tag in results_section.find_all("a", attrs={"data-job-id": True}):
            card = _parse_card(a_tag)
            if card and card["id"] not in seen_ids:
                seen_ids.add(card["id"])
                jobs.append(card)

    return jobs


def _fill_cache(timeout: int) -> None:
    r = _get(_INDIA_URL, timeout)
    jobs = _parse_page(r.text)
    _cache.extend(jobs)

    page = 2
    while page <= _MAX_PAGES:
        time.sleep(0.2)
        r = _get(f"{_INDIA_URL}/{page}", timeout)
        more_jobs = _parse_page(r.text)
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
    """Return a page of Comcast India job listings.

    Comcast's TalentBrew tenant ignores keyword/location query params on the
    India-facet URL (same confirmed-ignored shape as Disney's tenant), so
    every call returns the same India-only pool regardless of
    `keyword`/`location`; the shared matcher handles title/skill filtering
    afterward. The full pool is fetched (and paginated) once and cached
    in-module; `start`/`num` slice that cache so matcher.py's pagination
    loop terminates naturally once the cache is exhausted.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a Comcast job's full description + posting date.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block (same
    pattern as Disney/Boeing/Optum) — no browser needed.
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
