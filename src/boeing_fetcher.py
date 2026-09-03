"""Fetches Boeing India (BIETC) job listings from jobs.boeing.com (TalentBrew).

ATS identification (Step 1, verified live 2026-09-03 — do NOT trust a prior
"custom" assumption): jobs.boeing.com is TalentBrew, the same vendor/HTML
pattern already used by Optum (`optum_fetcher.py`) — confirmed via the
`tbcdn.talentbrew.com` CDN asset host, the `search-results__job-*` CSS class
names, the `/module/GetAutoCompleteKeyword` ajax endpoint, and a clean
schema.org `JobPosting` JSON-LD block on every job-detail page. A `Log in`
link on the landing page points at `boeing.wd1.myworkdayjobs.com`, and every
job-detail page's real "Apply" button also resolves there — Boeing's actual
application backend is Workday — but the entire searchable job list and full
description text is server-rendered on jobs.boeing.com itself, so no Workday
call is needed to fetch job data (same "TalentBrew frontend, different apply
backend" shape documented for Barclays, just confirmed harmless here since
TalentBrew genuinely serves the data we need).

Key finding, worth a "Key Bugs" entry: Boeing's TalentBrew tenant IGNORES the
free-text `l=` location query param entirely (`?l=India` on `/search-jobs`
still returns the full ~1000-job *global* pool, mostly US listings) — this is
the opposite of Optum's tenant, where `l=India` works. The only reliable way
to get India-only results is TalentBrew's dedicated location-facet page
(`/location/india-jobs/185/1269750/2`, the exact URL Boeing's own
`sitemap.xml` advertises as canonical for India). That facet page's own
`?k=`/`?p=`/`?page=`/`?pg=`/`?currentPage=` query params are ALSO all
ignored (confirmed empirically against a 543-job category page: every one of
those returned identical page-1 content) — real pagination on this ATS goes
through an undocumented POST ajax endpoint (`/search-jobs/resultspost`) that
was not reverse-engineered here, mirroring the SuccessFactors Unify
complexity already documented for Standard Chartered/Wipro/HCLTech. Since
Boeing's real India total is currently 2 (see below, well under the 15/page
default), this fetcher caches the single facet-page GET once per process and
does not attempt further pagination; if Boeing's India posting count ever
exceeds one page, `_fill_cache` prints a visible warning instead of silently
under-counting.

Live-verified 2026-09-03: the India-country facet (`facetTerm=1269750`,
`facetType=2`) returns exactly 2 open jobs — cross-checked identical at the
country, Karnataka-state, and Bengaluru-city facet levels, and again via a
combined Engineering-category + Bengaluru-location facet URL — so this is a
genuine current fact about Boeing's India posting volume, not an artifact of
one stale URL. Both current jobs ("Experienced ATE Mechanical Design
Engineer", "Experienced ATE Electrical Design Engineer") are hardware roles
that `matching.exclude_terms` already rejects (mechanical/electrical) before
any skill check runs, so 0 real `.NET/C#` or `AI/ML/Python` matches exist
today — same "confirmed-zero-is-a-real-fact" class as ING/eClerx/AIG/Swiss
Re/ANZ/Nasdaq/General Motors in this repo, not a fetcher defect. A web search
turned up several older-looking Boeing Bengaluru "Software Engineer"/
".NET Full Stack" postings; every one of their direct job-detail URLs 404s
live ("Custom Job Error") — this board's postings churn quickly, same
Infosys-style staleness already documented in the playbook, not a URL bug on
our side.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.boeing.com"
_ORG_ID = "185"
# Canonical India-country facet URL, taken verbatim from jobs.boeing.com's own
# sitemap.xml ("india-jobs" slug; facetTerm 1269750 = India, facetType 2 =
# country). Cross-verified identical at the Karnataka-state and
# Bengaluru-city facet levels — this is the one reliable India-only surface
# on this tenant (the free-text ?l=India param on /search-jobs is ignored
# server-side and returns the global pool instead).
_INDIA_URL = f"{_BASE_URL}/location/india-jobs/{_ORG_ID}/1269750/2"
_RECORDS_PER_PAGE = 15  # TalentBrew's default page size on this tenant

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


def _normalize_slash_date(date_str: str) -> str:
    """Convert TalentBrew's 'MM/DD/YYYY' search-result date to 'YYYY-MM-DD'."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", date_str.strip())
    if not m:
        return ""
    month, day, year = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


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
    """Parse one TalentBrew job `<a data-job-id>` (walking up to its `<li>`)."""
    job_id = (a_tag.get("data-job-id") or "").strip()
    if not job_id:
        return None

    href = a_tag.get("href", "")
    application_url = f"{_BASE_URL}{href}" if href.startswith("/") else href

    li = a_tag.find_parent("li")
    title = ""
    location = ""
    posting_date = ""
    if li is not None:
        title_span = li.find("span", class_="search-results__job-title")
        title = title_span.get_text(strip=True) if title_span else ""

        loc_span = li.find("span", class_="search-results__job-info location")
        location = loc_span.get_text(strip=True) if loc_span else ""

        date_span = li.find("span", class_="search-results__job-info date")
        if date_span:
            posting_date = _normalize_slash_date(date_span.get_text(strip=True))

    if not title:
        return None

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
                raise RateLimitError(f"Boeing: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Boeing fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Boeing fetch failed for {url}: {last_exc}")


def _fill_cache(timeout: int) -> None:
    r = _get(_INDIA_URL, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    section = soup.find("section", id="search-results")
    total_results = None
    if section is not None:
        raw_total = section.get("data-total-results", "")
        if raw_total.isdigit():
            total_results = int(raw_total)

    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for a_tag in soup.find_all("a", attrs={"data-job-id": True}):
        card = _parse_card(a_tag)
        if card and card["id"] not in seen_ids:
            seen_ids.add(card["id"])
            jobs.append(card)

    if total_results is not None and total_results > len(jobs) and total_results > _RECORDS_PER_PAGE:
        # Real pagination on this tenant requires an undocumented POST ajax
        # call (see module docstring) that was never reverse-engineered here.
        # Surface this loudly instead of silently under-counting.
        print(
            f"  [warn] Boeing India facet reports {total_results} total jobs "
            f"but only {len(jobs)} were parsed from one page — pagination past "
            f"page 1 is not implemented for this tenant; results are incomplete"
        )

    _cache.extend(jobs)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Boeing India (BIETC) job listings.

    Boeing's TalentBrew tenant ignores keyword/location query params on the
    India-facet page (confirmed empirically), so every call returns the same
    India-only pool regardless of `keyword`/`location`; the shared matcher
    handles title/skill filtering afterward. The full pool is fetched once
    and cached in-module; `start`/`num` slice that cache so matcher.py's
    pagination loop terminates naturally once the cache is exhausted.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a Boeing job's full description + posting date.

    Job-detail pages carry a clean schema.org JobPosting JSON-LD block
    (same pattern as Optum/SAP Labs/Schwab) — no browser needed.
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
