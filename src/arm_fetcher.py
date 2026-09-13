"""Fetches Arm Limited (Bangalore) job listings from careers.arm.com
(TalentBrew).

ATS identification (Step 1, verified live 2026-09-13, not guessed):
careers.arm.com serves plain server-rendered HTML whose search-results page
carries an `icims.com/connect` login link and `icims.com/jobs/login` — but
the actual job-search UI (`job-card`/`job-card__title` CSS classes,
`/en/search-jobs`/`/en/location/...` URL shapes, a `pagination-total-pages`
span) is unmistakably TalentBrew, the same vendor already confirmed for
Optum/Boeing/Disney/AstraZeneca in this repo — iCIMS almost certainly
underlies the apply/login flow (same "TalentBrew frontend over a different
apply backend" shape already documented for Barclays/GE Aerospace/GE
HealthCare), but the entire searchable job list and full description text is
served by TalentBrew's own HTML, so no separate API call is needed. Org ID
33099 (visible in every job-detail URL and the advanced-search form's hidden
`orgIds` field).

Same "free-text location param ignored" shape as Boeing/Disney/AstraZeneca:
`?Location=India`/`?Country=1269750` on `/en/search-jobs` both return the
unfiltered global pool (368 jobs at verification time, Austin/Cambridge/
Budapest-heavy). The only reliable India-only surface is TalentBrew's
dedicated location-facet path: `/en/location/india-jobs/33099/1269750/2`
(1269750 = India's TalentBrew/geonames ID, 2 = country facet type — the same
IDs Boeing's and Disney's tenants use; TalentBrew shares these across
customers). That facet page reports 101 India jobs.

Pagination on this tenant paginates cleanly via a path suffix, same
mechanism as Disney: `{india_facet_url}/{page}` (1-indexed; page 1 is also
the bare facet URL). `<span class="pagination-total-pages">of N</span>`
gives the true page count (7 at verification time, ~15-18 jobs/page).

Unlike Disney's/Boeing's tenant, job cards on this template put the title
directly inside the `<a class="job-card__title" data-job-id="...">` tag's
own text (no nested `<h2>`), and location in a sibling `<span
class="location">` within the same `<li class="job-card ...">` — a new
markup shape for this ATS vendor, worth remembering for future TalentBrew
integrations rather than assuming Disney's/Boeing's card structure. No
per-card posting-date element exists on this template at all (unlike
Disney's `job-date-posted` span) — `posting_date` is populated only from the
job-detail page's JSON-LD `datePosted` field, left `""` on the search-result
card itself.

Job-detail pages carry a clean schema.org `JobPosting` JSON-LD block, same
pattern as Boeing/Disney/AstraZeneca/SAP Labs/Schwab — full HTML description
+ `datePosted` ("YYYY-M-D") read from there, no browser needed.

Live data check (2026-09-13): Arm's India (Bangalore) pool is, as expected
for a chip-design IP licensor, majority ASIC/RTL/verification/SoC-modeling
hardware roles, but carries a genuinely large and real software/cloud/
platform-engineering track alongside it — "Staff Software Engineer - Linux
Kernel", "Principal Software Engineer" (×2), "Senior Software Engineer –
Networking for AI", "Staff DevOps Engineer (Data Storage)", "Staff Private
Cloud Engineer", and more — several matching the shared `title_family` list
directly (`software engineer`, `devops engineer`, `cloud engineer`). Live
JD checks on several of these (e.g. "Staff Private Cloud Engineer": Python,
Terraform/Ansible, OpenStack, FastAPI, CI/CD) found broad-only signals
(Python) but no hard `primary_skills` term today — a real "0 matches now,
strong pool for future matches" case, same class as Micron/Intel/TI in this
repo, not a fetcher defect. A handful of results returned by the India facet
page also spill in non-India postings (Austin, Cambridge, Lund) mixed among
the last few cards on some pages — `matcher.py`'s own `is_india_job()`
location-substring check correctly filters these out downstream, so no
special handling was added here beyond what every other fetcher already
relies on.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.arm.com"
_ORG_ID = "33099"
# Canonical India-country facet URL (1269750 = India, facetType 2 = country
# -- same TalentBrew-wide IDs used by Boeing's/Disney's tenants).
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
    """Parse one TalentBrew `<a class="job-card__title" data-job-id>` on this tenant."""
    job_id = (a_tag.get("data-job-id") or "").strip()
    if not job_id:
        return None

    href = a_tag.get("href", "")
    application_url = f"{_BASE_URL}{href}" if href.startswith("/") else href

    title = a_tag.get_text(strip=True)
    title = html_mod.unescape(title).replace("\xa0", " ")
    if not title:
        return None

    location = ""
    parent_li = a_tag.find_parent("li")
    if parent_li is not None:
        loc_span = parent_li.find("span", class_="location")
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
                raise RateLimitError(f"Arm: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Arm fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Arm fetch failed for {url}: {last_exc}")


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
        for a_tag in results_ul.find_all("a", class_="job-card__title"):
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
    """Return a page of Arm's India job listings.

    Arm's TalentBrew tenant ignores keyword/location query params on both
    `/en/search-jobs` and the India-facet URL (confirmed empirically — see
    module docstring), so every call returns the same India-only pool
    regardless of `keyword`/`location`; the shared matcher handles title/
    skill filtering afterward. The full pool is fetched (and paginated) once
    and cached in-module; `start`/`num` slice that cache so matcher.py's
    pagination loop terminates naturally once the cache is exhausted.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch an Arm job's full description + posting date.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block (same
    pattern as Boeing/Disney/AstraZeneca/SAP Labs/Schwab) — no browser needed.
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
