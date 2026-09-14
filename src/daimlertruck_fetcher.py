"""Daimler Truck AG job fetcher.

Daimler Truck (spun off from what is now Mercedes-Benz Group in 2021 -- a
DISTINCT company from ``mbrdi_fetcher.py``'s Mercedes-Benz R&D India, which
belongs to the passenger-car side) runs its own careers portal at
``jobsearch.daimlertruck.com``. India engineering presence lives inside
"Daimler Truck Innovation Center India" (DTICI, Bengaluru) and "Fuso Tech
Center India" (Chennai), both surfaced through this one global tenant.

ATS discovery (live, 2026-09-13): the visible portal is a PHP/jQuery
frontend (``index.php?ac=search_result``) that renders the search page
itself with zero jobs server-side -- the real search grid is filled by a
client-side XHR, found via Playwright network capture (not guessed from a
URL shape, per the playbook's standing "verify, don't guess ATS" lesson).
This turned out to be the SAME "milch & zucker GmbH Global Jobboard"/
Beesite product family already onboarded for Deutsche Bank
(``api-deutschebank.beesite.de``), Lufthansa, and MBRDI
(``jobs.api.mercedes-benz.com``) in this repo -- Daimler Truck's own tenant
host is ``global-jobboard-api-jobsearch.daimlertruck.com``, discovered only
via the captured request (the naive ``api-daimlertruck.beesite.de`` /
``daimlertruck.beesite.de`` guesses following the sibling tenants' naming
convention both fail to resolve).

Unlike the sibling tenants, THIS ONE's request shape is GET with the whole
JSON search body URL-encoded into a single ``data=`` query parameter (not a
POST JSON body) -- confirmed live via the same Playwright capture. Also
unlike MBRDI/Deutsche Bank, ``PublicationChannel.Code=12`` (the public
internet career-site channel) IS required as a ``SearchCriteria`` entry --
omitting it returns an unrelated/empty result set in testing.

No usable server-side country filter was found in the response fields the
frontend itself requests (India location live only inside
``PositionLocation[0].CountryName``); the whole small global pool (~327
postings at investigation time) is fetched in one ``CountItem=10000`` call
and filtered to India client-side -- same "cache once, don't re-page"
approach as MBRDI/Deutsche Bank/Lufthansa.

India presence confirmed genuinely NOT Chennai-only and NOT Pune (neither
excluded city dominates): of ~12 India postings at investigation time, most
are Chennai (Fuso Tech Center India -- already an excluded city), but real
non-excluded hits exist too: "Consultant" (Bangalore) and
"DTICI_Snowflake_Data_engineer_T8" (Bangalore, a genuine Snowflake/data-
engineering DTICI role). No Pune postings observed at all.

Job-detail pages (``index.php?ac=jobad&id={id}``) ARE plain server-rendered
HTML with a single ``application/ld+json`` ``JobPosting`` object (not a
``@graph``-wrapped array like MBRDI/Continental) -- ``description`` is
HTML-entity-escaped HTML (``&lt;h2&gt;...``), needs ``html.unescape`` BEFORE
stripping tags, not after (a single unescape pass is required, unlike a
plain HTML page's already-real tags).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
import urllib.parse

import requests

_SEARCH_HOST = "https://global-jobboard-api-jobsearch.daimlertruck.com"
_SEARCH_URL = f"{_SEARCH_HOST}/search/"
_DETAIL_BASE = "https://jobsearch.daimlertruck.com/index.php?ac=jobad&id="
_COUNT_ITEM = 10000  # whole known-small (~327) global pool in one call

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://jobsearch.daimlertruck.com/",
}

_DETAIL_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
}

_MATCHED_OBJECT_DESCRIPTOR = [
    "ID", "PositionTitle", "PositionURI",
    "PositionLocation.CountryName", "PositionLocation.CityName",
    "PublicationStartDate",
]

# Module-level cache -- filled once per process, reused for every keyword pass
# (this fetcher ignores `keyword`/`location`; see module docstring).
_india_cache: list[dict] = []
_cache_filled = False
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from Daimler Truck's Beesite tenant."""


def _fill_cache(timeout: int = 30) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before the fetch attempt -- avoid retry storms

    payload = {
        "LanguageCode": "EN",
        "SearchParameters": {
            "CountItem": _COUNT_ITEM,
            "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
            "MatchedObjectDescriptor": _MATCHED_OBJECT_DESCRIPTOR,
        },
        "SearchCriteria": [
            {"CriterionName": "PublicationChannel.Code", "CriterionValue": ["12"]}
        ],
    }
    url = _SEARCH_URL + "?data=" + urllib.parse.quote(json.dumps(payload))

    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Daimler Truck: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Daimler Truck cache fill failed: {exc}") from exc

    items = r.json().get("SearchResult", {}).get("SearchResultItems", [])

    collected: list[dict] = []
    for item in items:
        desc = item.get("MatchedObjectDescriptor", {})
        loc_list = desc.get("PositionLocation") or []
        if not loc_list or loc_list[0].get("CountryName") != "India":
            continue

        job_id = str(desc.get("ID") or "")
        title = (desc.get("PositionTitle") or "").strip()
        if not job_id or not title:
            continue

        city = (loc_list[0].get("CityName") or "").strip()
        location_str = f"{city}, India" if city else "India"
        posting_date = (desc.get("PublicationStartDate") or "")[:10]

        collected.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": f"{_DETAIL_BASE}{job_id}",
        })

    _india_cache = collected
    print(f"[DaimlerTruck] Cache filled: {len(_india_cache)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 30,
) -> list[dict]:
    """Return a page of Daimler Truck India job listings.

    Keyword/location are ignored -- the whole small global pool is cached
    once and filtered to India client-side (see module docstring).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 25) -> tuple[str, str]:
    """Fetch the description + posting date from the job-detail page's
    ``application/ld+json`` ``JobPosting`` block."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_DETAIL_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Daimler Truck detail: 429 on {application_url}")
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
    except (ValueError, TypeError):
        _desc_cache[application_url] = ("", "")
        return "", ""

    postings = ld_data if isinstance(ld_data, list) else [ld_data]
    posting = next((p for p in postings if p.get("@type") == "JobPosting"), None)
    if posting is None:
        _desc_cache[application_url] = ("", "")
        return "", ""

    raw_desc = html_mod.unescape(posting.get("description") or "")
    description = " ".join(re.sub(r"<[^>]+>", " ", raw_desc).split())
    posting_date = (posting.get("datePosted") or "")[:10]

    _desc_cache[application_url] = (description, posting_date)
    return description, posting_date
