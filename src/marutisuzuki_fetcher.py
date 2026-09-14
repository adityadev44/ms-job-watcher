"""Fetches Maruti Suzuki India job listings via the Param.ai career-page API.

ATS discovery (2026-09-13/14): marutisuzuki.com/corporate/careers links out to
a separate hosted candidate portal, ``maruti.app.param.ai/jobs/`` — **Param.ai**,
a new ATS vendor for this repo (an Indian HR/CRM platform, not previously seen
here). The public jobs page is a client-side-rendered Nuxt SPA
(`"data-ssr":"false"` in the page's own hydration payload) whose webpack bundle
was read directly to find the real endpoint:

    GET https://maruti.app.param.ai/api/career/get_job/
        -> {"data": {"<Department Name>": {"jobs": [...]}, ...}}

No auth, no session, no pagination — a single unauthenticated GET returns the
**entire company-wide open-requisition pool** (~900 postings as of
2026-09-14) grouped by department, across ~32 departments spanning
Engineering/Production/Quality Assurance/IT/Sales/etc. This is cached once per
process (Honeywell/Persistent lesson) and filtered to non-excluded India
locations client-side.

Each job dict's own `locations` field is a bare city name (e.g. "Gurgaon",
"Rohtak") with no "India" substring at all — `", India"` is appended before
handing off to matcher.py's `is_india_job()`/`exclude_locations` checks, same
pattern as Lenskart/Continental. The real India IT/software presence
concentrates in **Gurugram/Gurgaon** (Maruti Suzuki's corporate HQ), NOT
Pune -- Pune appears only 19 times out of ~900 total postings and even those
are line/plant-manufacturing roles, not software engineering. Live-verified
real matches: "Senior AI Engineer" (Gurgaon) explicitly names "Generative AI",
"Agentic AI", "RAG", "GraphRAG" in its JD body — a genuine `AI / ML / Python`
primary-skill hit (`generative ai`) reachable through the existing shared
`title_family`'s `"ai engineer"` phrase.

Other quirks:
- `id` is a UUID (Param.ai's own internal job id); `slug` is the
  human-readable URL segment used for the public apply page:
  `https://maruti.app.param.ai/jobs/{slug}` (route pattern `/jobs/:slug()`
  read directly out of the SPA's own webpack route table).
- `description` is full HTML (no truncation observed) already embedded in
  the list response — no separate detail-page fetch is needed. This fetcher
  caches id -> (description, posting_date) in-module during the same
  cache-fill pass as the job list, matching the Cognizant/Salesforce
  "everything inline" shape.
- `created_at` is already ISO-8601 (`"2026-09-08T05:30:53.669507Z"`) —
  truncate to the first 10 characters for `posting_date`.
- `slug` values are NOT guaranteed globally unique across departments in
  theory (Param.ai scopes them per-org, not per-department) but every
  observed collision-candidate in this dataset was distinct; `id` (the UUID)
  is used as the canonical seen-state key regardless.
"""

from __future__ import annotations

import html as html_mod
import re
import time

import requests

_JOBS_URL = "https://maruti.app.param.ai/api/career/get_job/"
_JOB_BASE = "https://maruti.app.param.ai/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


# Module-level cache: the whole company-wide job pool is fetched once and
# reused for every keyword/location call in this process (Honeywell lesson:
# _cache_filled is set to True *before* the fetch attempt so a transient
# failure doesn't retry-storm on every subsequent call).
_india_cache: list[dict] = []
_descriptions: dict[str, tuple[str, str]] = {}
_cache_filled: bool = False


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_JOBS_URL, headers=_HEADERS, timeout=max(timeout, 30))
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Maruti Suzuki: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Maruti Suzuki fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Maruti Suzuki: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Maruti Suzuki: invalid JSON — {exc}") from exc

    departments = payload.get("data") or {}
    collected: list[dict] = []
    for _dept_name, dept in departments.items():
        for j in dept.get("jobs", []):
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or "").strip()
            slug = (j.get("slug") or "").strip()
            if not (job_id and title and slug):
                continue

            locs = [l.strip() for l in (j.get("locations") or []) if l and l.strip()]
            if not locs:
                continue
            loc_str = "; ".join(locs)
            if not re.search(r"\bindia\b", loc_str, re.IGNORECASE):
                loc_str = f"{loc_str}, India"

            posting_date = (j.get("created_at") or "")[:10]
            app_url = f"{_JOB_BASE}/{slug}"

            collected.append({
                "id": job_id,
                "title": title,
                "location": loc_str,
                "posting_date": posting_date,
                "application_url": app_url,
            })
            _descriptions[job_id] = (_strip_html(j.get("description") or ""), posting_date)

    _india_cache[:] = collected
    print(f"[Maruti Suzuki] Cache filled: {len(collected)} jobs company-wide")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Maruti Suzuki postings from the cached full pool.

    keyword/location are accepted for interface compatibility but ignored —
    this API has no server-side filter at all; the whole company-wide pool
    (~900 jobs) is cached once and matcher.py's shared filters do the real
    title/skill/location work against the cached slice.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) from the in-module cache filled
    during fetch_jobs() — the full HTML description is already embedded in
    the list response, so no separate per-job request is needed.
    """
    for j in _india_cache:
        if j["application_url"] == application_url:
            return _descriptions.get(j["id"], ("", ""))
    return "", ""
