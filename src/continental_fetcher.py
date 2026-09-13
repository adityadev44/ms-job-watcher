"""Continental AG job fetcher — custom TYPO3 + Angular "jobportal" REST API.

ATS identification (Step 1, verified live 2026-09-13): jobs.continental.com
is NOT a third-party ATS vendor -- it is a custom Angular SPA ("jobportal",
served under TYPO3 CMS) with its own in-house search backend. The search
results page (jobs.continental.com/en/#/) is a pure client-side shell with
no server-rendered job data, so the real API had to be found by reading the
Angular bundle (jobportal/main.js) rather than guessing a vendor URL shape.

Trail (worth recording -- this took real reverse engineering, not a URL
guess): the page's own `robots.txt` disallows
`*/api/*/pagetype-jobs/$`, hinting at an `/api/{lang}/pagetype-jobs/` path
family. The actual landing page embeds
`window.conJobsConfigFile = '/en/api/configuration/pagetype-jobs/'`, a JSON
config endpoint that returns `{"endpoints": {"resultList":
"/en/api/result-list/pagetype-jobs/", ...}}`. The Angular bundle's
`getFormData()` builds a `multipart/form-data` POST body with keys prefixed
`tx_conjobs_api[...]` (`tx_conjobs_api[filter][searchTerm]`,
`tx_conjobs_api[itemsPerPage]`, `tx_conjobs_api[currentPage]`) and POSTs it
to `configuration.endpoints.resultList` -- confirmed live: a plain
`requests.post` with that multipart body (no cookies, no auth) returns full
JSON job data, `numFound`/`pagination`/`list` included.

Same "cache once, filter client-side" pattern as Deutsche Bank/Lufthansa/
Genpact: the `location` field returned per job (`countryLabel`, e.g.
"India", "Hungary", "United States") is a clean, already-resolved country
name with no per-tenant WID/facet needed, and the site-wide global job pool
is small (674 postings at investigation time, 7 pages of 100) -- cheaper and
more reliable than trying to reverse-engineer Continental's location-suggest
autocomplete (which uses opaque `locationSuggestChecksums` hashes, not a
plain country code, for server-side location filtering).

India presence confirmed genuinely NOT Pune-only: of 49 India postings found
at investigation time, the real IT/software roles are concentrated in
Bengaluru (Senior Software Engineer - Data Platform, Software Engineer -
Data Platform, IT engineer Data Lakehouse - Tech Lead, IT B2B/EDI
Developer, SAP S4 Hana SCM Architect); most of the remaining India postings
are Satara/Kalyani/Shirwal/Kesurdi plant-manufacturing roles (ContiTech
tire-manufacturing sites, not excluded cities) plus a handful of Kolkata/
Faridabad sales/ops roles (Kolkata already covered by `default_exclude_locations`).

Job-detail pages ARE server-rendered plain HTML (unlike the search-results
SPA shell) with a schema.org `@graph` JSON-LD block containing one
`JobPosting` entry -- no second API call, no Playwright needed for either
step.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_SEARCH_URL = "https://jobs.continental.com/en/api/result-list/pagetype-jobs/"
_PAGE_SIZE = 100
_MAX_PAGES = 20  # safety ceiling; pool was 7 pages (674 jobs) at investigation time

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://jobs.continental.com/en/",
}

_DETAIL_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
}

# Module-level cache -- populated once per process, reused for every keyword
# pass (this fetcher ignores `keyword`/`location`; see module docstring).
_india_cache: list[dict] = []
_cache_filled = False
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _fill_cache(timeout: int = 25) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    # Set before the fetch attempt (Honeywell/Lufthansa lesson) so a
    # transient failure doesn't trigger a retry storm across every keyword
    # pass in this scan cycle.
    _cache_filled = True

    collected: list[dict] = []
    page = 1
    try:
        while page <= _MAX_PAGES:
            data = {
                "tx_conjobs_api[itemsPerPage]": str(_PAGE_SIZE),
                "tx_conjobs_api[currentPage]": str(page),
            }
            for attempt in range(3):
                try:
                    r = requests.post(_SEARCH_URL, data=data, headers=_HEADERS, timeout=timeout)
                    if r.status_code == 429:
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        raise RateLimitError("Continental: 429 rate-limited")
                    r.raise_for_status()
                    break
                except RateLimitError:
                    raise
                except requests.RequestException as exc:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError(f"Continental search fetch failed: {exc}") from exc

            result = r.json().get("result", {})
            items = result.get("list", [])
            if not items:
                break

            for item in items:
                country = (item.get("countryLabel") or "").strip()
                if country.lower() != "india":
                    continue

                ref = str(item.get("refNumber") or item.get("uuid") or "")
                title = (item.get("title") or "").strip()
                if not ref or not title:
                    continue

                city = (item.get("cityLabel") or "").strip()
                location_str = f"{city}, India" if city else "India"

                app_url = item.get("absoluteUrl") or ""
                posting_date = (item.get("publicationDate") or "")[:10]

                collected.append({
                    "id": ref,
                    "title": title,
                    "location": location_str,
                    "posting_date": posting_date,
                    "application_url": app_url,
                })

            pagination = result.get("pagination", {})
            if pagination.get("isLastPage", True):
                break
            page += 1
            time.sleep(0.3)
    except RateLimitError:
        raise
    except Exception as exc:
        raise RateLimitError(f"Continental cache fill failed: {exc}") from exc

    _india_cache = collected
    print(f"[Continental] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 25,
) -> list[dict]:
    """Return a page of Continental India jobs.

    Keyword/location are ignored -- see module docstring for why the whole
    small global pool is cached once and filtered to India client-side.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 25) -> tuple[str, str]:
    """Fetch the full description from the job-detail page's JSON-LD `@graph` block."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_DETAIL_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Continental detail: 429 on {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException:
            if attempt < 2:
                time.sleep(1)
                continue
            _desc_cache[application_url] = ("", "")
            return "", ""

    match = re.search(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S
    )
    if not match:
        _desc_cache[application_url] = ("", "")
        return "", ""

    try:
        ld_data = json.loads(match.group(1))
    except json.JSONDecodeError:
        _desc_cache[application_url] = ("", "")
        return "", ""

    graph = ld_data.get("@graph", []) if isinstance(ld_data, dict) else ld_data
    for node in graph if isinstance(graph, list) else [ld_data]:
        if not isinstance(node, dict) or node.get("@type") != "JobPosting":
            continue
        description = _strip_html(node.get("description", ""))
        posting_date = (node.get("datePosted") or "")[:10]
        result = (description, posting_date)
        _desc_cache[application_url] = result
        return result

    _desc_cache[application_url] = ("", "")
    return "", ""
