"""Lufthansa Group job fetcher — milch & zucker "Global Jobboard" REST API.

The public careers site (apply.lufthansagroup.careers, aka
karriere.lufthansa.com) is a JS single-page shell: the search-results page
itself server-renders only a loading spinner, and job data is loaded
client-side from a separate API host, api-apply.lufthansagroup.careers.
Confirmed via a live Playwright network-request capture (not a URL-shape
guess — see the playbook's standing note on why "custom"/guessed ATS labels
have been wrong most of the time in recent onboarding waves).

The backend is "milch & zucker GmbH" Global Jobboard (self-identified in the
page footer, "powered by milch & zucker GmbH"). This is the SAME product
family as Deutsche Bank's Beesite API (api-deutschebank.beesite.de/search/,
see deutsche_fetcher.py) — identical request/response shape
(LanguageCode / SearchParameters{FirstItem,CountItem,Sort,
MatchedObjectDescriptor} / SearchCriteria), just a different tenant host and
field set. This fetcher follows the same "cache the whole small pool once,
filter India client-side" pattern for the same reason: no reliable
country-level search filter exists in the UI (the search form only offers a
geo-radius "distance from a point" location search, keyword full-text
search, brand/division, career level, etc. — no ISO-country facet), but the
entire current global job pool is small (~355 open reqs across the whole
Lufthansa Group) and each result item already carries a clean
PositionLocation.CountryCode field, so one uncapped fetch + a client-side
CountryCode == "IN" filter is simpler and more reliable than trying to coax
a real server-side India filter out of the geo-search UI.

Key findings from live investigation (2026-09-03):
- Plain `requests` works for both search and job-detail pages — no bot
  protection, no Playwright needed (unlike several other GCCs onboarded in
  the same wave, e.g. IBM/Honeywell/Tech Mahindra).
- Search API: GET https://api-apply.lufthansagroup.careers/search/
  with a `data=` query param containing URL-encoded JSON. Keyword/location
  criteria are IGNORED here on purpose — SearchCriteria is sent empty and
  the whole pool is cached once (~355 jobs at fetch time), same discipline
  as deutsche_fetcher.py's _fill_cache with the `_cache_filled = True`
  guard set *before* the fetch attempt (Honeywell/Persistent lesson: avoids
  a retry storm across every one of matcher.py's ~10 keyword passes if the
  API is briefly down, at the cost of that scan cycle silently seeing 0
  jobs — self-heals on the next 30-min run).
- Real India presence found: "Lufthansa Systems India Private Ltd."
  (Bangalore — a separate, smaller Lufthansa Group entity from Lufthansa
  Technik/Lufthansa Industry Solutions, both of which also have Bengaluru
  offices but 0 open reqs in this specific portal at investigation time)
  and occasional Lufthansa Cargo AG postings in Mumbai. Not Pune-only —
  no Pune postings observed at all.
- Job-detail pages (https://apply.lufthansagroup.careers/index.php?ac=jobad
  &id={id}) ARE plain server-rendered HTML (unlike the search-results page)
  with a single schema.org JobPosting JSON-LD block carrying the full,
  HTML-entity-encoded description and datePosted — no second API call
  needed, no Playwright needed.
"""
from __future__ import annotations

import html as _html_mod
import json
import re
import time

import requests

_SEARCH_URL = "https://api-apply.lufthansagroup.careers/search/"
_PAGE_SIZE = 1000  # covers the whole current pool (~355) in one request
_MAX_ITEMS = 5000  # safety ceiling if the pool ever grows past one page

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://apply.lufthansagroup.careers/",
}

_DETAIL_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
}

_SEARCH_DESCRIPTOR = [
    "ID",
    "PositionTitle",
    "PositionURI",
    "PositionLocation.CountryCode",
    "PositionLocation.CityName",
    "PublicationStartDate",
    "ParentOrganizationName",
]

# Module-level cache: populated on first call, reused for all keyword passes
# (the API ignores keyword/location filtering the way this fetcher uses it,
# so re-querying per keyword would just refetch the identical pool).
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _fill_cache(timeout: int = 30) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return

    # Set before the fetch attempt so a transient failure doesn't trigger a
    # retry storm across every keyword pass in this scan cycle (Honeywell/
    # Deutsche Bank/Persistent lesson — see module docstring).
    _cache_filled = True

    collected: list[dict] = []
    first_item = 1

    try:
        while first_item <= _MAX_ITEMS:
            payload = {
                "LanguageCode": "EN",
                "SearchParameters": {
                    "FirstItem": first_item,
                    "CountItem": _PAGE_SIZE,
                    "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
                    "MatchedObjectDescriptor": _SEARCH_DESCRIPTOR,
                },
                "SearchCriteria": [],
            }

            for attempt in range(3):
                try:
                    r = requests.get(
                        _SEARCH_URL,
                        params={"data": json.dumps(payload)},
                        headers=_HEADERS,
                        timeout=timeout,
                    )
                    if r.status_code == 429:
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        raise RateLimitError("Lufthansa search: 429 rate-limited")
                    r.raise_for_status()
                    break
                except RateLimitError:
                    raise
                except requests.RequestException as exc:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError(f"Lufthansa search fetch failed: {exc}") from exc

            data = r.json()
            result = data.get("SearchResult", {})
            items = result.get("SearchResultItems", [])
            if not items:
                break

            for item in items:
                desc = item.get("MatchedObjectDescriptor", {})
                loc_list = desc.get("PositionLocation", [])
                if not loc_list:
                    continue

                if loc_list[0].get("CountryCode", "") != "IN":
                    continue  # non-India — this fetcher only caches India jobs

                job_id = desc.get("ID") or item.get("MatchedObjectId", "")
                if not job_id:
                    continue
                job_id = str(job_id)

                title = (desc.get("PositionTitle") or "").strip()
                if not title:
                    continue

                city = (loc_list[0].get("CityName") or "").strip()
                location_str = f"{city}, India" if city else "India"

                application_url = desc.get("PositionURI") or (
                    f"https://apply.lufthansagroup.careers/index.php?ac=jobad&id={job_id}"
                )
                posting_date = (desc.get("PublicationStartDate") or "")[:10]

                collected.append({
                    "id": job_id,
                    "title": title,
                    "location": location_str,
                    "posting_date": posting_date,
                    "application_url": application_url,
                })

            total_claimed = result.get("SearchResultCountAll", 0)
            first_item += _PAGE_SIZE
            if first_item > total_claimed:
                break

    except RateLimitError:
        raise
    except Exception as exc:
        raise RateLimitError(f"Lufthansa cache fill failed: {exc}") from exc

    _india_cache = collected
    print(f"[Lufthansa] Cache filled: {len(collected)} India jobs from Global Jobboard API")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Lufthansa Group India jobs.

    Keyword/location are ignored server-side by this fetcher's own design
    (see module docstring) — the whole current India pool is small enough
    to cache once and slice locally.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full description from the job-detail page's JSON-LD block.

    Job-detail pages are plain server-rendered HTML (unlike the JS-only
    search-results shell) containing one schema.org JobPosting JSON-LD
    <script> block with the full HTML-entity-encoded description and
    datePosted.
    """
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_DETAIL_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Lufthansa detail: 429 on {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S
    )
    if not match:
        return "", ""

    try:
        ld_data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return "", ""

    postings = ld_data if isinstance(ld_data, list) else [ld_data]
    for posting in postings:
        if not isinstance(posting, dict):
            continue
        if posting.get("@type") != "JobPosting" and "description" not in posting:
            continue
        description = _strip_html(posting.get("description", ""))
        posting_date = (posting.get("datePosted") or "")[:10]
        return description, posting_date

    return "", ""
