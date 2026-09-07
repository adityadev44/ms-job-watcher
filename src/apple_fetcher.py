"""
Apple job fetcher — jobs.apple.com's own server-rendered search (no public
REST API docs; verified directly, not guessed).

`jobs.apple.com` is a React app, but it is genuinely server-rendered: a
plain unauthenticated `requests.get` on the search URL already embeds the
full, live search-results JSON in the HTML response, inside a
`window.__staticRouterHydrationData = JSON.parse("...")` script tag (a
React Router v7 "loader data" hydration payload — the outer JSON.parse
argument is itself a JSON-encoded string, so it must be unescaped twice:
once as a JS string literal, once as JSON). No Playwright needed anywhere
in this pipeline; confirmed live 2026-09-06.

    GET https://jobs.apple.com/en-us/search?location=india-INDC&search=<keyword>&page=<n>

`location=india-INDC` is Apple's internal country-location code for India
(found live in the page's own `queryParams` echo, not guessed) and is a
genuine server-side country filter — confirmed 176 total India postings,
`totalRecords` matching a separately-confirmed count. `page=` is a real
1-based page parameter, 20 results per page (confirmed: `page=2` returns
a disjoint set of 20 jobs from `page=1`).

`search=` (keyword) genuinely narrows server-side (0 results for a
nonsense token; "angular" -> 7; "dot net" -> 3) but is an OR-across-tokens
match, not an AND-phrase match — e.g. "C# developer" and ".NET developer"
both return the same count as bare "developer" alone (159), because
special-char tokens ("C#", ".NET") appear to be stripped by the search
index's tokenizer and only the plain word survives. This is the same
class of quirk already documented for Cisco (bare "AI" is a no-op there)
and TCS ("#" breaks the query outright) — genuine narrowing for real
tokens, just token-OR semantics rather than phrase-AND, so NOT registered
in `_IGNORES_KEYWORDS`.

**Known gap, not fixed here:** the search-results endpoint's per-job
`locations[]` array is always country-level ("India", blank city/state)
regardless of query — confirmed by also querying a specific city location
code (`location=bengaluru-BGS`) and finding the *response* still reports
blank city for every job. Real city (e.g. "Bengaluru, Karnataka") only
appears on the job *detail* page, which matcher.py only fetches for
candidates that already survived the location/exclude/title-family gate
using the list-stage `location` value. Consequence: config's default
`exclude_locations` (Pune, Chennai, etc.) cannot act on Apple postings —
every Apple India job's `location` is reported simply as `"India"`. This
is a genuine, deliberate limitation (flagged, not silently patched) —
matcher.py's own `is_india_job()` still works correctly since "India" is
present.

Pagination note: a live full-pool crawl (9 pages, `page=1..9`, no
keyword) returned 176 raw entries but only 163 unique `positionId`s —
some jobs shifted across page boundaries between requests (Apple's board
changes in near-real-time and default sort isn't strictly stable across
calls a few seconds apart). Deduplication by `id` in matcher.py already
absorbs this; the `_FIRST_PAGE_IDS` wraparound guard below is kept as a
defensive backstop in case of true pagination looping, and `totalRecords`
is also used as a hard stop once enough postings have been seen.

Live-verified 2026-09-06 totals: 163-176 India postings (see pagination
note above), 34 matching the standard keyword list — entirely in the
AI/ML/Python track (Machine Learning Engineer, Software Engineer variants,
Python Developer, AI/ML Software Engineer); zero .NET/C# matches (expected
— Apple does not run a .NET stack). This is Apple's real Hyderabad/
Bengaluru engineering + Apple Intelligence org, not a retail-only board —
titles include "Machine Learning Engineer, Apple Intelligence", "Senior
Software Engineer - AI Data Platform", "Software Engineer - Distributed
Systems".

Job detail: `GET https://jobs.apple.com/en-us/details/<positionId>/<slug>`
(the `transformedPostingTitle` field from the search result *is* the
slug) is the same server-rendered hydration pattern, under
`loaderData.jobDetails.jobsData`, with `description` +
`minimumQualifications` + `preferredQualifications` fields (concatenated
here) and a real per-job `postDateInGMT` (ISO-8601) — this is also where
the real city (e.g. "Bengaluru, Karnataka") lives, in
`jobsData.locations[0]`, unused for filtering per the gap noted above but
available if a future fix wants it.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_BASE_URL = "https://jobs.apple.com"
_SEARCH_URL = f"{_BASE_URL}/en-us/search"
_DETAIL_BASE = f"{_BASE_URL}/en-us/details"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_PAGE_SIZE = 20  # Apple's search-results page size (confirmed, not adjustable)

# Apple's internal country-location code for India (found live in the
# page's own queryParams echo).
_INDIA_LOCATION_CODE = "india-INDC"

_HYDRATION_MARKER = "__staticRouterHydrationData"
_PARSE_MARKER = 'JSON.parse("'

# Pagination-wraparound guard (per-call-sequence; reset naturally at start=0).
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network/parsing failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _extract_hydration_data(html_text: str) -> dict:
    """Pull the React-Router `__staticRouterHydrationData` payload out of a
    server-rendered jobs.apple.com page.

    The value is written as `JSON.parse("<escaped-json>")` — a JSON string
    literal (escaped once for the JS source) whose *content* is itself a
    JSON document, so it must be decoded twice: once to unescape the JS
    string literal, once to parse the resulting JSON text.
    """
    idx = html_text.find(_HYDRATION_MARKER)
    if idx == -1:
        raise ValueError("hydration data marker not found")
    sq = html_text.find(_PARSE_MARKER, idx)
    if sq == -1:
        raise ValueError("JSON.parse( not found after hydration marker")
    sq += len(_PARSE_MARKER)
    eq = html_text.find('")', sq)
    if eq == -1:
        raise ValueError("hydration data closing marker not found")
    raw_js_string = html_text[sq:eq]
    decoded_json_text = json.loads('"' + raw_js_string + '"')
    return json.loads(decoded_json_text)


def _get_with_retries(url: str, params: dict, timeout: int, label: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Apple {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Apple {label} failed: {exc}") from exc
    raise RateLimitError(f"Apple {label}: no response — {last_exc}")


def _location_param(location: str) -> str | None:
    if location and "india" in location.lower():
        return _INDIA_LOCATION_CODE
    return None


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return one page of Apple job listings from the server-rendered
    search-results page.

    `search` (keyword) and `location` (mapped to Apple's internal India
    country code) are both genuine server-side filters — see module
    docstring for the OR-token keyword-matching quirk. Pagination is
    1-based `page=`, fixed at 20 results/page; `start` is converted to a
    page number assuming callers always advance `start` by the previous
    page's actual length (matcher.py's convention).
    """
    global _FIRST_PAGE_IDS

    page_num = (start // _PAGE_SIZE) + 1
    params: dict[str, str | int] = {"page": page_num}
    if keyword:
        params["search"] = keyword
    loc_code = _location_param(location)
    if loc_code:
        params["location"] = loc_code

    r = _get_with_retries(_SEARCH_URL, params, timeout, "search")

    try:
        data = _extract_hydration_data(r.text)
        search = data["loaderData"]["search"]
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RateLimitError(f"Apple search: failed to parse hydration data — {exc}") from exc

    total_records = search.get("totalRecords") or 0
    results = search.get("searchResults") or []

    jobs: list[dict] = []
    for j in results:
        position_id = str(j.get("positionId") or "")
        title = (j.get("postingTitle") or "").strip()
        if not (position_id and title):
            continue

        slug = j.get("transformedPostingTitle") or ""
        application_url = f"{_DETAIL_BASE}/{position_id}/{slug}" if slug else f"{_DETAIL_BASE}/{position_id}"

        post_date_gmt = j.get("postDateInGMT") or ""
        posting_date = post_date_gmt[:10] if post_date_gmt else ""

        # locations[] is always country-level for this endpoint (see
        # module docstring) — every result here was requested with
        # location=india-INDC, so "India" is always correct.
        jobs.append({
            "id": position_id,
            "title": title,
            "location": "India",
            "posting_date": posting_date,
            "application_url": application_url,
        })

    # Pagination-wraparound guard.
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and jobs and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []

    # Hard stop once we've paged past the API's own reported total.
    if total_records and start >= total_records:
        return []

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date from the detail page's
    server-rendered hydration payload.

    Concatenates `description`, `minimumQualifications`, and
    `preferredQualifications` since real JD content is split across all
    three on this site.
    """
    r = _get_with_retries(application_url, {}, timeout, "description")

    try:
        data = _extract_hydration_data(r.text)
        job_data = data["loaderData"]["jobDetails"]["jobsData"]
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RateLimitError(f"Apple description: failed to parse hydration data — {exc}") from exc

    parts = []
    for key in ("description", "minimumQualifications", "preferredQualifications"):
        txt = job_data.get(key) or ""
        if txt:
            parts.append(_strip_html(txt))
    description = " ".join(parts)

    post_date_gmt = job_data.get("postDateInGMT") or ""
    posting_date = post_date_gmt[:10] if post_date_gmt else ""

    return description, posting_date
