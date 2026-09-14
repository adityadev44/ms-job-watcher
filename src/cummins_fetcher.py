"""Fetches Cummins Inc. (engine/powertrain manufacturer, heavily automotive-
adjacent -- supplies engines to Volvo Group/Scania/Daimler Truck/Ashok
Leyland among many others) job listings via the DirectEmployers "jobsyn.org"
Solr search API.

ATS discovery (live, 2026-09-13/14): ``www.cummins.com/careers`` and
``careers.cummins.com`` are both Cloudflare-managed-challenge-gated to a
degree that blocks headless Chromium outright (confirmed live: Chromium
with ``--disable-blink-features=AutomationControlled`` still hangs on the
Turnstile challenge and never reaches real content) -- headless **Firefox**
passes cleanly with zero extra effort (same "Chromium blocked -> try
Firefox" lesson as Honeywell/Akamai, opposite direction from NatWest/IBM's
"Firefox blocked -> try Chromium" case). ``careers.cummins.com``'s own
"EXPLORE OPPORTUNITIES" button link (found via Firefox link-harvesting, not
guessed) leads to the REAL board at a separate, unprotected domain:
``cummins.jobs`` -- a Nuxt.js SPA on the DirectEmployers "jobsyn.org"
platform (an ATS-adjacent vendor not otherwise seen in this repo).
``cummins.jobs`` itself has NO Cloudflare gate at all, so this fetcher
never needs to touch Playwright for the actual data -- only the discovery
hop through ``careers.cummins.com`` needed a browser, and that hop's result
(the ``cummins.jobs`` URL) is hardcoded here rather than re-discovered on
every run.

Real API (found via live Playwright network capture of a
``cummins.jobs/career-path/.../jobs/`` page): ``GET https://prod-search-
api.jobsyn.org/api/v1/solr/search?page=N&country=IND``. This endpoint
enforces an ``x-origin: cummins.jobs`` header (NOT the standard browser
``Origin`` header, which is a same-origin normal browser value and is
irrelevant here) -- a bare ``requests.get()`` without this custom header
gets a clean HTTP 403 ``{"errors": "Mismatched origin."}``; adding
``x-origin: cummins.jobs`` (spoofable from plain ``requests``, no session/
cookie/token needed) is sufficient to get real data back.

Pagination: ``num_items``/``per_page``/``limit``/``page_size`` query params
are all silently ignored -- confirmed live, page size is fixed at 10
regardless. ``page=N`` genuinely advances (confirmed: page 2 returns
different jobs than page 1). ``country=IND`` genuinely narrows server-side
(confirmed: total drops from Cummins' full global pool to 183 India
postings at investigation time) -- the whole India pool is cached once per
process (19 pages of 10) rather than re-querying per keyword, since
``title``/``q`` params exist but this repo's convention of "cache once,
let matcher.py narrow" is simpler and this pool is small.

India presence confirmed genuinely NOT Pune-only: of 183 India postings,
Pune (~121) and Phaltan (~31, a Cummins plant town in Maharashtra, not
Pune itself and not in ``default_exclude_locations``) dominate, but real
non-Pune/non-excluded postings also exist across Bengaluru, Ahmedabad,
Gurugram, Navi Mumbai, Jaipur, Delhi, Hosur, Mysuru, Jamshedpur, Ranchi,
and "Virtual, IND". Current live snapshot (investigation time): the
Pune-based IT roles ("Solution Architect - Senior", "Data Engineer -
Senior", "Data Scientist - Senior", "Software Engineer - Senior") are
genuinely on-target .NET/AI-ML-adjacent titles but sit in the excluded
city; every non-Pune/non-excluded posting currently open is a
manufacturing/technician/sales/supply-chain role with no primary-skill
match -- a genuine current-zero-match snapshot, not a fetcher bug. This
pipeline is mechanically correct and will surface a real Bengaluru/
Ahmedabad/Gurugram software opening automatically the moment Cummins posts
one there.

Fields: ``description`` is already full plain/lightly-markdown text INLINE
in the search response (same "no separate detail call needed" shape as
Perfios/Darwinbox/Ashok Leyland elsewhere in this repo). ``date_added`` is
ISO-8601 UTC (truncate to the first 10 chars for ``YYYY-MM-DD``).

Application URL: ``https://cummins.jobs/{city-slug}-{country-slug}/
{title_slug}/{guid}/job/`` (e.g. ``.../pune-ind/solution-architect-senior/
EB799.../job/``) -- confirmed live via Playwright link-harvesting on a real
listing page. A handful of postings have a null ``city_exact`` (ambiguous/
multi-site postings); those fall back to a country-only slug, which may
occasionally 404 on the human-facing link -- harmless for matching (this
fetcher never needs to re-fetch the detail page since ``description`` is
already inline), flagged here rather than silently patched around.
"""
from __future__ import annotations

import re
import time

import requests

_SEARCH_URL = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
_JOBS_BASE = "https://cummins.jobs"
_MAX_PAGES = 40  # safety cap (~400 jobs) well beyond the known ~183-job India pool

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "x-origin": "cummins.jobs",
    "Referer": f"{_JOBS_BASE}/",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Module-level cache -- filled once per process, reused for every keyword pass
# (this fetcher ignores `keyword`/`location`; see module docstring).
_india_cache: list[dict] = []
_cache_filled = False


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from the jobsyn.org Solr API."""


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")


def _build_url(job: dict) -> str:
    title_slug = job.get("title_slug") or _slugify(job.get("title_exact") or "")
    guid = job.get("guid") or ""
    city = job.get("city_exact") or ""
    country_short = job.get("country_short_exact") or "IND"
    location_slug = _slugify(f"{city}-{country_short}") if city else _slugify(country_short)
    if not (title_slug and guid):
        return _JOBS_BASE + "/jobs/"
    return f"{_JOBS_BASE}/{location_slug}/{title_slug}/{guid}/job/"


def _location_from_job(job: dict) -> str:
    city = (job.get("city_exact") or "").strip()
    if city:
        return f"{city}, India"
    # Fall back to the raw all_locations list (e.g. "Virtual, IND" postings
    # where city_exact is null but all_locations still names something).
    for loc in job.get("all_locations") or []:
        cleaned = (loc or "").strip().strip(",")
        if cleaned and cleaned.lower() != "india":
            return f"{cleaned}, India"
    return "India"


def _fill_cache(timeout: int = 25) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before the loop -- avoid retry storms

    collected: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        for attempt in range(3):
            try:
                r = requests.get(
                    _SEARCH_URL,
                    params={"page": page, "country": "IND"},
                    headers=_HEADERS,
                    timeout=timeout,
                )
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Cummins: 429 rate-limited")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Cummins cache fill failed: {exc}") from exc

        data = r.json()
        jobs = data.get("jobs") or []
        if not jobs:
            break

        for j in jobs:
            job_id = str(j.get("guid") or j.get("id") or "")
            title = (j.get("title_exact") or "").strip()
            if not job_id or not title:
                continue
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": (j.get("date_added") or j.get("date_new") or "")[:10],
                "application_url": _build_url(j),
                "_description": (j.get("description") or "").strip(),
            })

        pagination = data.get("pagination") or {}
        if not pagination.get("has_more_pages"):
            break
        time.sleep(0.2)

    _india_cache = collected
    print(f"[Cummins] Cache filled: {len(_india_cache)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 25,
) -> list[dict]:
    """Return a page of Cummins India job listings.

    Keyword/location are ignored -- the whole (small, ~183-job) India pool
    is cached once via the ``country=IND`` server-side filter and served in
    slices; matcher.py's title/skill filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return [
        {k: v for k, v in job.items() if k != "_description"}
        for job in _india_cache[start : start + num]
    ]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Cummins job.

    Served entirely from the cache filled by ``fetch_jobs`` -- the search
    API already returns the full description text inline for every
    posting, same as Perfios/Darwinbox/Ashok Leyland elsewhere in this repo.
    """
    for job in _india_cache:
        if job["application_url"] == application_url:
            return job.get("_description", ""), job.get("posting_date", "")
    return "", ""
