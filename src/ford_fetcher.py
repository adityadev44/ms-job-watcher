"""Ford Motor Company (Ford Business Solutions India) job fetcher — TalentBrew.

ATS identification (Step 1, verified live 2026-09-13): careers.ford.com is
TalentBrew, the same vendor/HTML pattern already used by Boeing/Optum/Disney
in this repo -- confirmed via the `tbcdn.talentbrew.com` CDN asset host and
the `search-results-list__*` CSS class names on the search-results page.

Same "India facet page, cache once" shape as `boeing_fetcher.py`: the free-
text `Country=` query param on `/search-jobs` is silently IGNORED when
combined with a job-category filter (confirmed: `?ac=9255664&Country=1269750`
returns a ~75-job, mostly-US result set unrelated to the country param), but
the canonical India-country facet URL
(`/search-jobs?acm=ALL&alrpm=1269750&ascf=[{"key":"ALL","value":""}]`,
found by following the site's own `/india` landing page link chain, where
`1269750` is TalentBrew's cross-tenant India country-facet ID also seen at
Boeing) returns a clean, India-only result set. Ford's `k=` keyword param
does narrow results, but only fuzzily (an OR-ish match, not a phrase filter
-- `k=software engineer` still surfaces "Business Analyst"/"SAP Consultant"
postings with no literal keyword overlap), so this fetcher caches the whole
India facet page once per process rather than trusting per-keyword filtering,
same reasoning as Lufthansa/Deutsche Bank/MBRDI's cache-once fetchers.

Verified current fact, not a fetcher defect: Ford's ENTIRE India job pool is
very small (9 open postings at investigation time, comfortably under the
15/page TalentBrew default, so no pagination is needed) and skews SAP/
finance/business-analyst -- e.g. "SAP IHC Functional Consultant", "Senior
SAP S/4 HANA Tax Consultant", "Business Analyst". Exactly one posting,
"Senior Kubernetes/OpenShift Platform Engineer" (location "India, Remote"),
is software/platform-engineering-adjacent, and "Manager, Middleware
Engineering (Accounts Payable & Tax)" is excluded by `matching.exclude_terms`
as a manager title regardless. Included anyway for completeness -- same
precedent as General Motors/BNY Mellon/Icertis (a real, working, low-volume
India board, not a structural zero) -- rather than skipped, since the ATS is
fully queryable and Ford's own "Enterprise Technology" job category
(id 9255664) does exist and could post India roles at any time; near-zero
current match volume is a fact about today's postings, not about fetcher
coverage.

Most India postings on this tenant are Chennai (`default_exclude_locations`
already covers Chennai) and Coimbatore (not excluded, but the two Coimbatore/
Sanand postings seen are Asset Management/Logistics roles with zero
overlap with `matching.primary_skills` regardless).

Job-detail pages ARE plain server-rendered HTML with a single schema.org
`JobPosting` JSON-LD block (posting date in a non-zero-padded "YYYY-M-D"
format, e.g. "2026-9-10") -- same shape as Boeing, no Playwright needed for
either step.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://www.careers.ford.com"
_INDIA_URL = (
    f"{_BASE_URL}/search-jobs?acm=ALL&alrpm=1269750"
    '&ascf=[%7B%22key%22:%22ALL%22,%22value%22:%22%22%7D]'
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_cache: list[dict[str, str]] = []
_cache_filled = False
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


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


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Ford: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Ford fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Ford fetch failed for {url}: {last_exc}")


def _parse_card(a_tag: Any) -> dict[str, str] | None:
    job_id = (a_tag.get("data-job-id") or "").strip()
    href = a_tag.get("href", "")
    title = a_tag.get_text(strip=True)
    if not job_id or not title:
        return None

    application_url = f"{_BASE_URL}{href}" if href.startswith("/") else href

    h2 = a_tag.find_parent("h2")
    location = ""
    if h2 is not None:
        info_list = h2.find_next_sibling("ul", class_="search-results-list__job-info-list")
        if info_list is not None:
            loc_li = info_list.find("li", class_="job-location")
            if loc_li is not None:
                location = loc_li.get_text(strip=True)

    return {
        "id": job_id,
        "title": title,
        "location": location or "India",
        "posting_date": "",  # populated by fetch_job_description
        "application_url": application_url,
    }


def _fill_cache(timeout: int) -> None:
    global _cache_filled
    if _cache_filled:
        return
    # Set before the fetch attempt (Honeywell lesson) so a transient failure
    # doesn't trigger a retry storm across every keyword pass this cycle.
    _cache_filled = True

    r = _get(_INDIA_URL, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for a_tag in soup.find_all("a", class_="search-results-list__job-link"):
        card = _parse_card(a_tag)
        if card and card["id"] not in seen_ids:
            seen_ids.add(card["id"])
            jobs.append(card)

    heading = soup.find(class_="search-results__heading")
    if heading is not None:
        m = re.search(r"(\d+)\s+Results", heading.get_text())
        if m and int(m.group(1)) > len(jobs):
            print(
                f"  [warn] Ford India facet reports {m.group(1)} total jobs "
                f"but only {len(jobs)} were parsed from one page — this "
                f"fetcher does not paginate past page 1 for this tenant"
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
) -> list[dict]:
    """Return a page of Ford India jobs.

    Keyword/location are ignored -- Ford's own keyword search is a loose,
    unreliable fuzzy match (see module docstring), and the whole current
    India pool is small enough to cache once from the canonical country-
    facet URL and slice locally.
    """
    _fill_cache(timeout=timeout)
    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    try:
        r = _get(application_url, timeout)
    except RateLimitError:
        raise
    except Exception:
        _desc_cache[application_url] = ("", "")
        return "", ""

    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S
    )
    if not match:
        _desc_cache[application_url] = ("", "")
        return "", ""

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        _desc_cache[application_url] = ("", "")
        return "", ""

    postings = data if isinstance(data, list) else [data]
    for posting in postings:
        if not isinstance(posting, dict) or "description" not in posting:
            continue
        description = _strip_html(posting.get("description", ""))
        posting_date = _normalize_ld_date(posting.get("datePosted", ""))
        result = (description, posting_date)
        _desc_cache[application_url] = result
        return result

    _desc_cache[application_url] = ("", "")
    return "", ""
