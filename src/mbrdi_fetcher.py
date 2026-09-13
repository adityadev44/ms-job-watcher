"""Mercedes-Benz Research and Development India (MBRDI) job fetcher.

MBRDI's public postings live inside the parent Mercedes-Benz Group careers
site (jobs.mercedes-benz.com), not a standalone portal -- there is no
separate "MBRDI-only" ATS tenant. That site is a Nuxt.js SPA whose search
page ships almost no server-rendered content; the real backend, found by
reading the SPA's own bootstrap config
(`window.__NUXT__.config.public.gjbAddress`), is
`https://jobs.api.mercedes-benz.com` -- the "milch & zucker GmbH Global
Jobboard" product family (self-identified via the `beesite` string in the
bootstrap JS bundle). This is the SAME product family as Deutsche Bank's
Beesite API (`api-deutschebank.beesite.de/search/`) and Lufthansa's Global
Jobboard tenant (`api-apply.lufthansagroup.careers/search/`) already in
this repo -- same `LanguageCode`/`SearchParameters`/`SearchCriteria` request
shape, a different tenant host and field set.

Same "cache the whole pool once, filter client-side" pattern as those two
sibling fetchers, for the same reason: `SearchCriteria` country filters
(`PositionLocation.Country`/`PositionLocation.CountryCode`) are silently
IGNORED server-side on this tenant -- confirmed live: a POST with
`SearchCriteria: [{"CriterionName": "PositionLocation.CountryCode",
"CriterionValue": ["IN"]}]` returns the identical unfiltered global mix
(Germany/US/China postings) as no criteria at all. The global pool
(~2,557 postings at investigation time) is paged with `CountItem=500`
(the tenant 500s on `CountItem=1000`) and each item's own
`PositionLocation[0].CountryCode` field is checked client-side instead.

India presence confirmed genuinely NOT Pune-only, and explicitly NOT the
"Pune-only GCC" case flagged for exclusion in an earlier onboarding wave:
of 221 India postings found at investigation time, ~206 are Bengaluru/
Bangalore (MBRDI's HQ) and only ~15 are Pune (already covered by
`default_exclude_locations`). Real, genuine primary_skills matches exist in
the description text, not just the title -- confirmed live on a real
job-detail page: "CAD customization using .NET (C#), VB" names ".NET (C#)"
directly in its JD body ("Develop standalone/integrated desktop
applications, Macros & Plugins for CAD customization using .NET (C#), VB
or other relevant languages"). Other India titles seen: "Fullstack
Developer - .NET ITO Transition" (x2 variants), "AI Senior Engineer - ITO
Transition", "Senior Data Engineer", "Full stack Developer - Java/Vue/AI",
"DevOps & CI-CD Architect".

Job-detail pages (`https://jobs.mercedes-benz.com/{slug}-{id}-{ref}`) ARE
plain server-rendered HTML (unlike the JS-only search shell), each carrying
one `application/ld+json` block shaped as a schema.org `@graph` array (NOT
a bare `JobPosting` object like Lufthansa's) -- the `JobPosting` entry has
to be picked out of the graph by `@type`, same "@graph"-wrapped shape
Continental's TYPO3 jobportal also uses.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_SEARCH_URL = "https://jobs.api.mercedes-benz.com/search/"
_PAGE_SIZE = 500  # tenant returns HTTP 500 on CountItem=1000; 500 is safe
_MAX_ITEMS = 20000  # safety ceiling well above the ~2,557-job pool observed

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://jobs.mercedes-benz.com/",
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


def _fill_cache(timeout: int = 30) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    # Set before the fetch attempt (Honeywell/Lufthansa lesson) so a
    # transient failure doesn't trigger a retry storm across every keyword
    # pass in this scan cycle.
    _cache_filled = True

    collected: dict[str, dict] = {}
    first_item = 1
    try:
        while first_item <= _MAX_ITEMS:
            payload = {
                "LanguageCode": "EN",
                "SearchParameters": {
                    "FirstItem": first_item,
                    "CountItem": _PAGE_SIZE,
                    "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
                },
                "SearchCriteria": [],
            }
            for attempt in range(3):
                try:
                    r = requests.post(_SEARCH_URL, json=payload, headers=_HEADERS, timeout=timeout)
                    if r.status_code == 429:
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        raise RateLimitError("MBRDI: 429 rate-limited")
                    r.raise_for_status()
                    break
                except RateLimitError:
                    raise
                except requests.RequestException as exc:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError(f"MBRDI search fetch failed: {exc}") from exc

            result = r.json().get("SearchResult", {})
            items = result.get("SearchResultItems", [])
            if not items:
                break

            for item in items:
                desc = item.get("MatchedObjectDescriptor", {})
                loc_list = desc.get("PositionLocation") or []
                if not loc_list or loc_list[0].get("CountryCode") != "IN":
                    continue

                job_id = str(desc.get("ID") or item.get("MatchedObjectId") or "")
                title = (desc.get("PositionTitle") or "").strip()
                if not job_id or not title:
                    continue

                city = (loc_list[0].get("CityName") or "").strip()
                location_str = f"{city}, India" if city else "India"

                app_url = desc.get("PositionURI") or ""
                posting_date = (desc.get("PublicationStartDate") or "")[:10]

                # Same job can appear multiple times across publication
                # channels; keep the first (newest, since sorted DESC).
                collected.setdefault(job_id, {
                    "id": job_id,
                    "title": title,
                    "location": location_str,
                    "posting_date": posting_date,
                    "application_url": app_url,
                })

            total_claimed = result.get("SearchResultCountAll", 0)
            first_item += _PAGE_SIZE
            if first_item > total_claimed:
                break
            time.sleep(0.3)
    except RateLimitError:
        raise
    except Exception as exc:
        raise RateLimitError(f"MBRDI cache fill failed: {exc}") from exc

    _india_cache = list(collected.values())
    print(f"[MBRDI] Cache filled: {len(_india_cache)} India jobs from Mercedes-Benz Global Jobboard")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 30,
) -> list[dict]:
    """Return a page of MBRDI (Mercedes-Benz Group India) jobs.

    Keyword/location are ignored -- see module docstring for why the whole
    global pool is cached once and filtered to India client-side.
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
                raise RateLimitError(f"MBRDI detail: 429 on {application_url}")
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
