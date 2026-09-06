"""Fetches Allianz Technology India job listings — Phenom People, SSR JSON,
NO Playwright needed (server-rendered, plain `requests` suffices).

careers.allianz.com is Phenom People, tenant "AISAIPGB" (confirmed via
`cdn.phenompeople.com/CareerConnectResources/AISAIPGB/...` asset URLs and
`phApp.refNum == "AISAIPGB"` embedded in the raw HTML). This is Allianz
Group's single umbrella careers site — Allianz Technology, Allianz Partners,
Allianz Services, Allianz Life, etc. all post through the same board and are
told apart only by each job's own `unit` field (see "Business-unit scoping"
below).

CORRECTING TWO PRIOR MIS-STEPS ON THIS COMPANY (see PLAYBOOK.md's Batch 5
entries):

1. A first attempt wrongly called this "infeasible" because a cold plain
   `requests.get()` on the *client-side widget API*
   (`/api/apply/v2/jobs`) returns `{"status":"failure","errorMsg":"Tenant
   not identified"}`. That endpoint is the Vue SPA's own in-page `fetch()`
   target and does need a live browser session — but it is simply the WRONG
   endpoint to use at all, same lesson as this repo's other Phenom tenants
   (Cisco/United Airlines): there is a second, much simpler avenue.

2. A second attempt found `phApp.sessionParams.csrfToken` embedded in the
   home page's HTML and was about to test replaying it as a header/param
   against the widget API with a captured cookie — reasonable next step,
   but unnecessary. **No token or cookie of any kind is needed.** Confirmed
   live with a completely cold, single-shot `requests.get()` (no prior
   request, no session, no cookie jar, no CSRF header) directly against:

       GET https://careers.allianz.com/global/en/search-results
           ?keywords=<term>&from=<offset>

   This returns HTTP 200 with the full result set embedded as a JSON blob
   assigned to `phApp.ddo` in the raw server-rendered HTML — exactly the
   same "SSR JSON" shape this repo already uses for Cisco/United Airlines/
   Google. The widget API's "Tenant not identified" error is irrelevant to
   this working path; it is never called here. Zero Playwright, zero
   cookies, zero tokens.

Real "API" (just a normal SSR HTML page, parsed the same way as Cisco):

    GET /global/en/search-results?keywords=<term>&from=<offset>
        -> phApp.ddo.eagerLoadRefineSearch = {"totalHits": N,
             "data": {"jobs": [{reqId, title, unit, country, city,
                                 cityStateCountry, multi_location,
                                 postedDate, descriptionTeaser, ...}]}}

Country/location facet — tested live, genuinely does NOT work here:
  - `location=India` collapses to 0 hits (same "needs a resolved lat/long,
    not a plain string" behavior as GE Aerospace's Phenom-over-Workday skin
    in this repo — Phenom's own geocoding widget, not a text filter).
  - `country=India`, `multi_location=India`, `locations=India` are ALL
    silent no-ops — every one returns `totalHits: 1634` (the full,
    unfiltered global board), identical to no param at all. Confirmed by
    also trying combined phrases like `keywords="engineer india"`, which
    return 0 (this ATS's multi-word `keywords=` is a literal contiguous-
    phrase match, not a per-token AND across fields — appending "india" to
    a real search term breaks it rather than narrowing it).
  - What DOES work: `keywords=India` alone (a bare literal-text query) —
    genuinely narrows server-side to `totalHits: 40`. Verified this is
    EXACTLY the true India set and not a coincidental/leaky text match: a
    full independent crawl of the entire unfiltered global board (1634
    jobs, ~164 raw pages, filtering client-side on each job's own `country`
    field) produced the identical 40 `reqId`s, zero set difference either
    direction. So `keywords=India` is used here as the sole scoping query
    (page size fixed at 10, like Cisco/United — `num`/`hitsPerPage`
    overrides are not attempted, same known-ignored idiom as those two).
  - Per-call `keyword`/`location` arguments from matcher.py are therefore
    IGNORED entirely (same "unreliable multi-word ATS keyword search" call
    as Darwinbox/Morningstar/ING elsewhere in this repo) — this fetcher
    always internally queries the fixed literal `keywords=India`, paginates
    that scoped result set to exhaustion ONCE per process, caches it, and
    serves slices. Confirmed several of this repo's real default keyword
    phrases (".NET developer", "C# developer", "dot net", "python
    developer", "generative ai engineer") return `totalHits: 0` against
    this ATS even though jobs mentioning those individual words in
    isolation do exist ("python" alone -> 11 hits) — this ATS's multi-word
    `keywords=` requires the exact phrase to appear contiguously somewhere
    in the indexed text, so relying on it per-keyword would silently MISS
    real matches. matcher.py's own title_family/skills substring checks
    (run after this fetcher returns the full India pool) do the real
    narrowing instead, exactly like Darwinbox.

Business-unit scoping (Allianz Technology, not the whole Allianz Group):
  Of the 40 group-wide India postings, each job's own `unit` field splits
  cleanly into "Allianz Technology" (28), "Allianz Partners" (7), and
  "Allianz Services" (5) — confirmed live, no ambiguous/blank values. Since
  this fetcher's assigned company is specifically Allianz Technology (not
  the Allianz Group umbrella board), only `unit == "Allianz Technology"`
  (case-insensitive) jobs are kept — 28 postings as of 2026-09-06. If a
  future re-check wants the broader Allianz Group board instead, this is
  the one filter to relax.

Title artifact: every raw title here carries an internal dedup suffix —
`_<digits>`, `_D<digits>`, or `_D-<digits>` (e.g. `"Group Technical
Architect_1876"`, `"Senior Java AI Developer_D-2678"`) — stripped by
`_clean_title()` for both matching and display. Purely cosmetic/precision
cleanup: matcher.py's substring-based title_family check would still match
through the raw suffix since it's appended, not embedded, but stripping it
gives cleaner notification titles and near-miss log lines.

Location: `cityStateCountry` is a clean "City, State, India" string with no
stray postal-code tail (unlike `location`, which sometimes carries a
trailing ZIP, e.g. "Pune, Maharashtra, India, 411014"); preferred here for
that reason, same class of "prefer the clean field over the messy one" call
as several Workday tenants in this repo. Country-only postings (no city)
render as a bare "India".

Description: NOT inline in the search response (`descriptionTeaser` is a
~250-400 char marketing blurb, same as Cisco). `fetch_job_description` hits
the job's own Phenom detail page (`/global/en/job/{reqId}/{any-slug}` —
confirmed the server resolves purely by the `{reqId}` path segment; a
deliberately wrong slug still 200s with the correct job, same as Cisco/
United) and reads the single schema.org `JobPosting` JSON-LD block. Its
`description` field is HTML-entity-encoded HTML (`&lt;p ...&gt;`) — unescape
once, then strip tags.

Bug avoided (same class as Cisco/United's entry in PLAYBOOK.md's Key Bugs
table): the JSON-LD detail page's own `datePosted` is NOT the real posting
date — sampled reqId 100227, whose search-response `postedDate` reads
2026-06-08 but whose detail-page JSON-LD `datePosted` reads 2026-07-31
(drifted toward "recently", not accurate). `fetch_job_description`
deliberately returns "" for the date so matcher.py keeps the accurate
`posting_date` already set in `fetch_jobs()`.

Application URL: the Phenom job detail page above is used as
`application_url` (not the raw `applyUrl`/`imApplyUrl`, which points
straight at Allianz's real underlying ATS, SAP SuccessFactors —
`career5.successfactors.eu/careers?company=AZGROUPPROD&career_job_req_id=
{reqId}...`, confirmed present on every job record). The Phenom page itself
carries the full JD (needed for `fetch_job_description` anyway) and its own
"Apply" action forwards to that same SuccessFactors URL, so nothing is lost
by linking there instead — same choice this repo already makes for Cisco/
United Airlines.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_BASE_URL = "https://careers.allianz.com"
_SEARCH_URL = f"{_BASE_URL}/global/en/search-results"
_JOB_BASE = f"{_BASE_URL}/global/en/job"

# The only query text confirmed to genuinely narrow this ATS's search server-
# side to India (see module docstring) — NOT the per-call keyword argument.
_INTERNAL_KEYWORD = "India"
_TENANT_UNIT = "allianz technology"  # compared lower-cased against job["unit"]

_SITE_PAGE_SIZE = 10  # fixed by the ATS; num/hitsPerPage overrides are ignored
_MAX_PAGES = 20  # safety cap — board is ~40 raw India hits, ~4 real pages

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.experimentData", re.S)
_LDJSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)
_TITLE_SUFFIX_RE = re.compile(r"_\s*D?-?\d+\s*$")


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Allianz's careers site."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _clean_title(raw_title: str) -> str:
    """Strip this ATS's internal dedup suffix (``_1876``, ``_D2881``,
    ``_D-2678``, even with a stray space before the digits) from a job
    title. See module docstring."""
    title = html_mod.unescape(raw_title or "").strip()
    title = _TITLE_SUFFIX_RE.sub("", title).strip()
    return title


def _slugify(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-")
    return slug or "job"


def _location_from_job(job: dict) -> str:
    """Prefer the clean ``cityStateCountry`` field — ``location`` on this
    tenant sometimes carries a stray trailing ZIP code (e.g. "Pune,
    Maharashtra, India, 411014")."""
    loc = (job.get("cityStateCountry") or job.get("location") or "").strip()
    if not loc:
        return "India"
    if "india" not in loc.lower():
        loc = f"{loc}, India"
    return loc


def _get(url: str, params: dict | None, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"429 rate-limited fetching {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Allianz Technology fetch failed: {exc}") from exc
    raise RateLimitError(f"Allianz Technology fetch: no response -- {last_exc}")


def _fetch_ssr_page(offset: int, timeout: int) -> tuple[list[dict], int]:
    """One plain, cold, unauthenticated request to the SSR search-results
    page at a given offset. No cookies/CSRF/session needed — see module
    docstring. Returns (jobs_on_this_page, total_hits_reported_by_the_ATS)
    for the fixed ``keywords=India`` scoping query."""
    params = {"keywords": _INTERNAL_KEYWORD, "from": str(offset)}
    r = _get(_SEARCH_URL, params, timeout)

    m = _DDO_RE.search(r.text)
    if not m:
        return [], 0
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return [], 0

    block = data.get("eagerLoadRefineSearch", {}) or {}
    total_hits = block.get("totalHits", 0) or 0
    jobs = ((block.get("data") or {}).get("jobs")) or []
    return jobs, total_hits


# ---------------------------------------------------------------------------
# Job-list cache — paginate the India-scoped query once per process
# ---------------------------------------------------------------------------

_job_cache: list[dict] = []
_cache_filled: bool = False

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


def _fill_cache(timeout: int = 20) -> None:
    """Paginate the fixed ``keywords=India`` query to exhaustion, keep only
    ``unit == "Allianz Technology"`` postings, and cache the result.

    ``_cache_filled`` is set True before the loop so a transient failure
    does not trigger a retry-storm on every subsequent fetch_jobs() call in
    the same process (Honeywell/CRED/Razorpay/Darwinbox lesson — see
    PLAYBOOK.md's Key Bugs table).
    """
    global _cache_filled, _job_cache, _FIRST_PAGE_IDS
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None
    total_expected: int | None = None
    offset = 0

    for page_num in range(_MAX_PAGES):
        raw_jobs, total_hits = _fetch_ssr_page(offset, timeout)
        if total_expected is None:
            total_expected = total_hits
        if not raw_jobs:
            break

        page_ids = {str(j.get("reqId") or "") for j in raw_jobs if j.get("reqId")}
        if page_num == 0:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1 — stop here

        raw_new = 0
        for j in raw_jobs:
            req_id = str(j.get("reqId") or "").strip()
            if not req_id or req_id in seen_ids:
                continue
            seen_ids.add(req_id)
            raw_new += 1

            unit = (j.get("unit") or "").strip().lower()
            if unit != _TENANT_UNIT:
                continue  # a different Allianz brand's posting — not ours

            title = _clean_title(j.get("title") or "")
            if not title:
                continue

            collected.append({
                "id": req_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": (j.get("postedDate") or "")[:10],
                "application_url": f"{_JOB_BASE}/{req_id}/{_slugify(title)}",
            })

        offset += len(raw_jobs)

        if raw_new == 0:
            break  # every id on this page already seen — dedupe/wraparound guard
        if total_expected is not None and offset >= total_expected:
            break
        if len(raw_jobs) < _SITE_PAGE_SIZE:
            break
        time.sleep(0.1)  # be polite between pages

    collected.sort(key=lambda j: j["posting_date"], reverse=True)
    _job_cache = collected
    print(f"[Allianz Technology] Cache filled: {len(collected)} total jobs")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py / run_company.py
# ---------------------------------------------------------------------------


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a cached slice of Allianz Technology's India postings.

    ``keyword``/``location`` are accepted for interface compatibility but
    IGNORED — this ATS's own multi-word ``keywords=`` search requires an
    exact contiguous phrase match and silently returns 0 hits for several
    of this repo's real default keywords even when matching jobs exist
    (see module docstring), so relying on it per-call would create false
    negatives. Instead the entire Allianz-Technology-scoped India pool
    (~28 postings) is paginated once per process via a single fixed
    internal ``keywords=India`` query (confirmed to exactly match a full
    independent board crawl) and cached; matcher.py's own title/skill/India
    filters do the real narrowing.
    """
    if not _cache_filled:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                _fill_cache(timeout=timeout)
                break
            except RateLimitError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            if last_exc:
                raise RateLimitError(
                    f"Allianz Technology cache fill failed: {last_exc}"
                ) from last_exc

    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single job by reading
    the schema.org JobPosting JSON-LD block off its Phenom detail page.

    Deliberately returns "" for the date — this page's own JSON-LD
    ``datePosted`` drifts toward "recently" rather than the real posting
    date (confirmed live; see module docstring). Returning "" leaves
    fetch_jobs()'s accurate ``posting_date`` untouched (matcher.py only
    overwrites it with a non-empty value).
    """
    r = _get(application_url, None, timeout)

    m = _LDJSON_RE.search(r.text)
    if not m:
        return "", ""
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return "", ""

    description = _strip_html(data.get("description") or "")
    return description, ""
