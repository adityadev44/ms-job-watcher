"""Fetches Synopsys job listings from careers.synopsys.com (TalentBrew).

ATS identification (Step 1, verified live 2026-09-14, not guessed):
careers.synopsys.com/search-jobs serves plain server-rendered HTML built on
TalentBrew (`tbcdn.talentbrew.com`, org id 44408 in every job-card/detail
URL) — the same vendor already confirmed for Arm/Boeing/Disney/AstraQeneca
in this repo. The entire searchable job list and full description text is
served by TalentBrew's own HTML, so no separate API call is needed.

Same "dedicated location-facet path" shape as Arm: the free-text location
param on `/search-jobs` is unreliable, but TalentBrew's canonical India
facet URL works cleanly: `/en/location/india-jobs/44408/1269750/2`
(1269750 = India's TalentBrew/geonames ID, 2 = country facet type — the
same cross-customer IDs Arm's tenant uses). That facet page reports 117
India jobs across 8 pages, paginated via a path-appended page number
(`{india_url}/{page}`, 1-indexed, same mechanism as Arm/Disney).

Markup on this tenant differs from Arm's: job cards are
`<li class="search-results-list__list-item">` containing
`<a class="sr-job-link" href="..." data-job-id="...">` whose own `<h2>`
holds the title, and a sibling `<span class="job-location">` holds the
location — closer to Disney's nested-`<h2>` shape than Arm's flat
`job-card__title` text. No per-card posting-date element; `posting_date`
is populated only from the job-detail page's JSON-LD `datePosted`.

**Important 2026 context**: Synopsys completed its ~$35B acquisition of
Ansys in July 2025. As of this onboarding, `ansys.com/careers` redirects to
`ansys.synopsys.com/careers`, which serves the *same* TalentBrew org 44408
tenant this fetcher already covers (confirmed live: Ansys product names
like "STK" and "ODTK" — Ansys's satellite toolkit products — appear in job
titles returned by this same India-facet URL). Ansys is therefore NOT
onboarded as a separate company in this repo — it would be a fully
redundant fetcher over the same job pool (same disposition as the Siemens
Mobility / Siemens AG precedent elsewhere in this repo).

Live data check (2026-09-14): Synopsys's India (Bengaluru/Hyderabad/Pune/
Noida) pool is, as expected for an EDA company, majority ASIC/verification/
analog-design/DFT hardware-adjacent roles, but carries a real software
track: "Senior Staff R&D Engineer – C/C++, Python, Unix/Linux, scripting",
"Staff R&D Software Engineer - DFT", "Site Reliability, Staff / HPC
Infrastructure Engineer", "Principal Engineer, Agentic AI (CAD/EDA)"
(explicitly builds LLM/agent orchestration for EDA workflows), and "AI, Sr
Architect". Several of these don't currently pass `title_family` (bare
"R&D Engineer"/"Architect" titles aren't covered — same precision gap
already flagged for Broadcom/TI in this repo), so a 0-or-low current match
count is expected and genuine, not a fetcher defect.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.synopsys.com"
_ORG_ID = "44408"
# Canonical India-country facet URL (1269750 = India, facetType 2 = country
# -- same TalentBrew-wide IDs used by Arm's/Boeing's/Disney's tenants).
_INDIA_URL = f"{_BASE_URL}/en/location/india-jobs/{_ORG_ID}/1269750/2"
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
    """Parse one `<a class="sr-job-link" data-job-id>` card on this tenant."""
    job_id = (a_tag.get("data-job-id") or "").strip()
    if not job_id:
        return None

    href = a_tag.get("href", "")
    application_url = f"{_BASE_URL}{href}" if href.startswith("/") else href

    h2 = a_tag.find("h2")
    title = h2.get_text(strip=True) if h2 else a_tag.get_text(strip=True)
    title = html_mod.unescape(title).replace("\xa0", " ")
    if not title:
        return None

    location = ""
    loc_span = a_tag.find_next("span", class_="job-location")
    if loc_span:
        location = loc_span.get_text(strip=True)

    return {
        "id": job_id,
        "title": title,
        "location": location or "India",
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
                raise RateLimitError(f"Synopsys: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Synopsys fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Synopsys fetch failed for {url}: {last_exc}")


def _parse_page(html: str) -> tuple[list[dict[str, str]], int]:
    soup = BeautifulSoup(html, "html.parser")

    total_pages = 1
    pages_span = soup.find("span", class_="pagination-total-pages")
    if pages_span:
        m = re.search(r"(\d+)", pages_span.get_text())
        if m:
            total_pages = int(m.group(1))

    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for li in soup.find_all("li", class_="search-results-list__list-item"):
        a_tag = li.find("a", class_="sr-job-link")
        if not a_tag:
            continue
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
    """Return a page of Synopsys's India job listings.

    This TalentBrew tenant ignores keyword/location query params (same
    empirically-confirmed behavior as Arm's tenant), so every call returns
    the same India-only pool regardless of `keyword`/`location`; the shared
    matcher handles title/skill filtering afterward. The full pool is
    fetched (and paginated) once and cached in-module.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a Synopsys job's full description + posting date.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block (same
    pattern as Arm/Boeing/Disney/AstraZeneca) — no browser needed.
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
