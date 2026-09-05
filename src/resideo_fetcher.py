"""
Resideo job fetcher — a Webflow Cloud proxy API in front of Oracle Fusion HCM.

Resideo Technologies (spun off from Honeywell in 2018; smart-home/security
brands Honeywell Home, First Alert, Resideo) does NOT run its real careers
search on the branded corporate domain (`www.resideo.com/us/en/corporate/
careers/` 404s outright — that path is a stale Sitecore-CMS link). The real
careers site is a *separate* subdomain, `www.careers.resideo.com`, linked
from the corporate homepage footer. That subdomain is itself built on
Webflow (`cdn.prod.website-files.com`, `x-wf-page-id` response header) — a
marketing/CMS site, not an ATS.

Live investigation (2026-09-05) found the backing ATS two layers down:
- The Webflow homepage's own "Join our Talent Community" link points at
  `https://ehtl.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/
  CX/join-talent-community` — confirming Oracle Fusion HCM Recruiting Cloud
  (Oracle HCM CE) as the actual backing ATS, same platform family already in
  this repo via Hexaware/Chubb/Icertis/WTW/eClerx/DTCC/BNY/Amex/Dell. HOWEVER
  calling that tenant's own `recruitingCEJobRequisitions` REST endpoint
  directly with `siteNumber=CX` (the URL's own site slug) returns
  `TotalJobsCount: 0` — the public site slug is not the API's real
  `siteNumber` value, and the correct value was never found (unlike every
  other Oracle HCM CE tenant in this repo, guessing further wasn't needed —
  see below).
- The Webflow site's `/career-search` landing page loads a small custom
  bundle, `careercategorymap-1.2.1.js`, which does `fetch("/app/api/jobs")`
  to populate its department tile counts. That path is a Webflow Cloud
  serverless function hosted on the same `www.careers.resideo.com` origin
  (not a third-party host) that itself proxies/caches the real Oracle HCM
  CE data server-side and re-serves it as a single flat JSON array with
  clean field names and the FULL job description already inlined — this is
  the actual integration point the live career-search page depends on, and
  the one this fetcher talks to directly. A near-identical shape to the
  Salesforce lesson already in this repo's PLAYBOOK ("public careers site
  doesn't call its own ATS's search API at all") — except here the
  in-between hop is a first-party Webflow Cloud function, not a static CDN
  export, and it is the ONLY working path found (the real Oracle tenant/
  site-number pair was never recovered).

Verified live via direct GET requests (2026-09-05):
- `GET https://www.careers.resideo.com/app/api/jobs` (no auth, no params)
  returns `{"jobs": [...]}`, 63 total postings company-wide right now.
- Every query-string variant tried (`?country=India`, `?page=2`,
  `?limit=500`, `?size=200`, `?all=true`) returned the byte-identical 63
  jobs — the endpoint takes no keyword/location/pagination params at all
  and always returns its full current pool in one shot. Two repeated plain
  calls also returned the identical 63 job IDs (not a random/paginated
  sample). Registered in `_IGNORES_KEYWORDS` for this reason; there is no
  location facet to register either — every call just re-serves the same
  cached snapshot, so `keyword`/`location` are accepted for interface
  compatibility only.
- 7 of the 63 are India postings, all in "BANGALORE METROPOLITAN AREA,
  KARNATAKA" — confirmed via each job's own structured `country`/`state`/
  `city` fields (not a text guess): 2 Marketing, 2 Information Technology
  (one Sr Systems Administrator, one Sr Advanced Cyber Security Architect/
  Engineer), 3 Engineering (Sr Advanced Software Engineer, Sr Software
  Engineer, and "Backend -.net/C# and Azure Engineer" — the last one
  explicitly naming "C#/.NET Core" in its own body text). A genuinely small
  but real India engineering presence, not a marketing-only board.

Field mapping: `name` (Oracle requisition number, e.g. "300026087197346")
is used as the stable job id — it's what's embedded in the job's own
`slug`/`detailUrl`, unlike the `id` field (an opaque Webflow document id
with no public meaning). `title`/`city`/`state`/`country` are plain strings.
`detailUrl` is a path on this same origin (`/jobs/<slug>`), confirmed to
serve a real HTTP 200 job detail page — `applyUrl` is NOT job-specific (every
job carries the identical generic `https://www.resideo.com/us/en/careers/`
value), so `detailUrl` is what this fetcher builds `application_url` from.
`datePosted` is an empty string on every one of the 63 jobs with no
alternative date field anywhere in the payload — `posting_date` is reported
as `""` throughout (matcher.py sorts empty-date jobs as oldest, and the
notifier/caller only overwrite a cached date if the value is truthy, both
already handled by the existing contract).

Description handling — genuinely inline, no per-job detail HTTP call needed:
`description` itself is always an empty string (dead field on this
endpoint), and `aiSnippet` is a short 1-2 sentence AI-generated teaser
(~400-500 chars) — NEITHER is the real JD. The full human-written JD
(JOB DUTIES / YOU MUST HAVE / WE VALUE / WHAT'S IN IT FOR YOU sections) is
concatenated onto the END of `searchExtras`, Resideo's own client-side
full-text search index field (`id + slug + normalised-slug + aiSnippet +
full JD text`, verified by direct field-length inspection: `aiSnippet` is
469 chars but `searchExtras` for the same job is 6247 chars). No HTML tags
were found in any `searchExtras` value across the 63-job pool, so no
tag-stripping is needed — just whitespace normalisation. This fetcher caches
the whole (India-only) pool once per process and serves both `fetch_jobs`
and `fetch_job_description` from that cache, same cache-once idiom as
`swiggy_fetcher.py`/`simcorp_fetcher.py` — there is no per-job detail
endpoint to fall back to even if one were wanted, since `/app/api/jobs` is
the only integration point this investigation found.
"""
from __future__ import annotations

import time

import requests

_SITE_BASE = "https://www.careers.resideo.com"
_JOBS_API = f"{_SITE_BASE}/app/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_SITE_BASE}/career-search",
}

# Module-level cache: the whole current job pool is fetched once per process
# (the endpoint ignores every query param and always returns everything —
# see module docstring) and reused for every keyword/location call, same
# idiom as swiggy_fetcher.py/simcorp_fetcher.py.
_india_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent failure fetching Resideo's jobs API."""


def _clean_text(raw: str) -> str:
    return " ".join((raw or "").replace("\xa0", " ").split())


def _build_location(job: dict) -> str:
    city = (job.get("city") or "").strip()
    state = (job.get("state") or "").strip()
    country = (job.get("country") or "").strip()
    parts = [p.title() for p in (city, state) if p]
    parts.append(country or "India")
    return ", ".join(parts)


def _fill_cache(timeout: int = 20) -> None:
    """Fetch Resideo's full current job pool once and cache the India subset.

    _cache_filled is set before the request attempt so a transient failure
    doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent/SimCorp lesson — see PLAYBOOK "Key Bugs"); a cycle that hits
    an error here simply yields no Resideo jobs this cycle and self-heals on
    the next scheduled run.
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_JOBS_API, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Resideo: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Resideo cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Resideo cache fill: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Resideo cache fill: invalid JSON — {exc}") from exc

    raw_jobs = payload.get("jobs") or []
    collected: list[dict] = []
    for j in raw_jobs:
        country = (j.get("country") or "").strip()
        if country.lower() != "india":
            continue  # conservative: only the tenant's own structured country field counts

        job_id = str(j.get("name") or "").strip()
        title = (j.get("title") or "").strip()
        detail_url = (j.get("detailUrl") or "").strip()
        if not (job_id and title and detail_url):
            continue

        app_url = f"{_SITE_BASE}{detail_url}"

        search_extras = j.get("searchExtras") or ""
        _desc_cache[app_url] = _clean_text(search_extras)

        collected.append({
            "id": job_id,
            "title": title,
            "location": _build_location(j),
            "posting_date": "",  # no date field available anywhere on this API
            "application_url": app_url,
        })

    _india_cache[:] = collected
    print(f"[Resideo] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Resideo's India job postings.

    keyword/location are accepted for interface compatibility but ignored:
    `/app/api/jobs` has no keyword/location/pagination params at all —
    verified empirically (every query-string variant tried returned the
    byte-identical full pool) — so the whole India-filtered pool is cached
    once and sliced here.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Resideo job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    the jobs-list response already carries each job's full JD text (in
    `searchExtras`, see module docstring), so no separate detail HTTP call
    is made or even available.
    """
    _fill_cache(timeout=timeout)
    description = _desc_cache.get(application_url, "")
    return description, ""
