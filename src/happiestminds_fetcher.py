"""Happiest Minds job fetcher — Zwayam ATS, shared multi-tenant endpoint.

Happiest Minds' obvious corporate domain (`happiestminds.com`) has no
`/careers/` page of its own -- it 301-redirects visitors to a distinct
subdomain, `careers.happiestminds.com`, whose own "Apply Now" link points at
yet another host, `jobs.happiestminds.com/happiestminds/`. That third page is
an Angular SPA (confirmed by inspecting the bundle directly, not assumed from
branding) whose `environment` config embeds `APIENDPOINT` /
`APIENDPOINTNEW: "https://public.zwayam.com/"` and `COMPANYID:
"MTU5NzQ="` (base64 for `"15974"`) -- the same shared Zwayam multi-tenant
endpoint (`public.zwayam.com`) already used by `crisil_fetcher.py` and
`persistent_fetcher.py` in this repo, confirmed live by the page's own
"Powered by Zwayam" footer link and a successful direct `POST
public.zwayam.com/jobs/search` with this companyId. This fetcher reuses the
exact request shape CRISIL's already-verified integration uses.

Verified via direct requests against the live API (2026-09-06):
- 360 total postings company-wide, 323 tagged India (`text8` / "Work
  Location Country" facet: 315 plain `"INDIA"` + 5 `"INDIA, INDIA, INDIA"`
  + 3 `"INDIA, INDIA, INDIA"`-style repeats for jointly-posted multi-city
  reqs -- no job mixes an India segment with a non-India one, so a simple
  `"india" in text8.lower()` check is a fully reliable India gate here,
  unlike most tenants in this repo that must infer country from city text
  alone).
- `anyOfTheseWords` (keyword) genuinely narrows server-side (0 for a
  nonsense token, 76/360 for "engineer", 61/360 for "python", 16/360 for
  ".net") -- but, matching the documented Zwayam pattern (see CRISIL/
  Persistent in PLAYBOOK.md), it is NOT used: the full India pool is cached
  once per process and matcher.py's title/skill filters do the real
  narrowing, avoiding any keyword-matching surprises. Register this slug in
  `company_registry.py`'s `_IGNORES_KEYWORDS` alongside `crisil`/
  `persistent`.
- Pagination: server-enforced page size is a fixed 10
  (`facetedSearchConfig.paginationHowMuch`), not configurable via any
  request parameter (identical to CRISIL). `hasMoreData` correctly goes
  `false` exactly at the true total (verified directly at start=350 and
  start=360) -- no wraparound observed, unlike several other tenants in
  this repo.
- Titles are ALL-CAPS by convention here (e.g. "CLOUD ARCHITECT - Ansible",
  "SENIOR SOFTWARE ENGINEER - Embedded C++") -- harmless, since
  `title_family`/`exclude_terms` matching is already case-normalised.

Location handling -- the same joint-multi-city-string leak class already
documented for Eurofins/Energy Exemplar/CRISIL, a new concrete instance:
`locationSeparatedbySlash` is comma-joined here (not "/"-joined like CRISIL's
tenant), e.g. `"Bengaluru, Noida, Pune"`, `"Noida, Bengaluru, Hyderabad"` --
7 real current India postings use this joint form, all combining a
non-excluded city (Bengaluru/Noida/Hyderabad) with excluded Pune. The
per-job `jobLocationRecord` list is unreliable here (it inconsistently
carries only one, sometimes-wrong, city -- e.g. it names "Pune" alone for a
posting whose real display string is "Bengaluru, Noida, Pune"), so it is
NOT used for anything beyond a last-resort fallback. Instead,
`locationSeparatedbySlash` is split on commas and the first segment that
matches none of the standard excluded-city tokens is kept (same
`_pick_city_segment` pattern as `eurofins_fetcher.py`), so a real Bengaluru/
Noida req isn't dropped purely because Pune is also jointly listed. The
chosen segment gets ", India" appended before being handed to matcher.py,
since `is_india_job()` requires the literal substring "india" in the
location string and bare city names here never contain it.

Job descriptions are NOT reliably inline: `mediumDescriptionWithoutHtml` in
the search response is empty for 114 of 360 (~32%) real postings (verified
directly, not assumed) -- same shape as CRISIL/Persistent on this ATS
family, so every description is fetched from the separate detail endpoint
(`POST /jobs-service/v1/jobs/careersite`) for every job, never read inline.
That endpoint reliably returns full HTML in `longDescription` for both
search-empty and search-populated cases (spot-checked one of each) -- this
field, not the shorter `mediumDescriptionWithoutHtml` also present in the
detail response, is used for full-text `primary_skills` matching.
`companyId` in the detail request must be the *decoded* `"15974"`, not the
base64 `COMPANYID` form used in the search request -- same asymmetry as
CRISIL.

Two data-quality notes, deliberately not "fixed" (real upstream data, not a
fetcher bug):
- One posting (id 1167737, "ASSOCIATE ARCHITECT - Azure Cloud") carries a
  malformed `createDate` of `"16-Feb-0030"` in the live tenant's own data
  (parses to year 0030) -- `_parse_date` has no year-sanity check, so this
  job simply sorts as the oldest result rather than crashing or being
  dropped; harmless for matching, just noted in case it looks alarming in a
  future log.
- A real posting ("SENIOR SOFTWARE ENGINEER ENGINEERING AND BUSINESS
  EXCELLENCE", id 1154557 -- actually an Azure DevOps Engineer role per its
  own JD) gets tagged `[.NET / C#]` purely because its "Preferred Skills"
  section lists "programming languages (e.g., Python, Java, C#)" as one
  example among several, not because the role uses .NET/C# at all -- the
  same class of substring false-positive already documented for Walmart's
  "languages we also use" JD boilerplate (Wave 5) and Citi's "SQL Server"
  mistag, and equally not fixable via `require_tech_in_description` (the
  same "C#" substring would still match). Not worth enabling strict
  filtering for -- see the onboarding report for the full reasoning.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime
from json import dumps as _json_dumps

import requests

_CAREERS_BASE = "https://jobs.happiestminds.com/happiestminds"
_API_BASE = "https://public.zwayam.com"
_SEARCH_URL = f"{_API_BASE}/jobs/search"
_DETAIL_URL = f"{_API_BASE}/jobs-service/v1/jobs/careersite"
_COMPANY_ID_B64 = "MTU5NzQ="  # base64("15974")
_COMPANY_ID = "15974"
_MAX_PAGES = 200  # safety ceiling; server page size is a fixed 10 (~360 jobs)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_CAREERS_BASE}/",
    "Origin": "https://jobs.happiestminds.com",
}

# Mirrors config.yaml's `_defaults.exclude_locations` anchor. Duplicated here
# (not imported) because fetchers are self-contained modules with no access
# to per-run config -- same reasoning as eurofins_fetcher.py/
# energyexemplar_fetcher.py -- used only to pick a sane segment out of a
# joint multi-city posting before handing off to matcher.py, which applies
# the real config-driven exclusion.
_EXCLUDED_CITY_TOKENS = [
    "chennai", "tamil nadu", "pune", "chandigarh", "kochi", "kerala",
    "trivandrum", "lucknow", "nagpur", "madurai", "kolkata", "indore",
]

_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-Mon-YYYY' (e.g. '05-Sep-2026') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _pick_city_segment(raw_loc: str) -> str:
    """Reduce a joint multi-city string ("Bengaluru, Noida, Pune") to one
    segment, preferring the first that mentions none of the standard
    excluded-city tokens -- so a job jointly posted in a valid city and an
    excluded one isn't dropped purely because the excluded city is *also*
    listed (same reasoning as eurofins_fetcher.py's `_pick_city_segment`).
    If every segment is excluded (or there's only one), the original string
    is returned unchanged.
    """
    segments = [s.strip() for s in raw_loc.split(",") if s.strip()]
    if len(segments) < 2:
        return raw_loc
    for seg in segments:
        if not any(tok in seg.lower() for tok in _EXCLUDED_CITY_TOKENS):
            return seg
    return raw_loc


def _location_from_job(src: dict) -> str:
    """Build the location string handed to matcher.py.

    `locationSeparatedbySlash` (comma-joined here) is the primary source;
    `jobLocationRecord`'s `formattedLocation` is a last-resort fallback only
    (verified unreliable as a primary source -- see module docstring).
    ", India" is appended so matcher.py's `is_india_job()` substring check
    passes, since bare city names never say "India" themselves.
    """
    raw_loc = (src.get("locationSeparatedbySlash") or "").strip()
    if not raw_loc:
        records = src.get("jobLocationRecord") or []
        formatted = [r.get("formattedLocation") for r in records if r.get("formattedLocation")]
        return " / ".join(formatted) if formatted else ""

    city = _pick_city_segment(raw_loc)
    if not city or city.lower() == "india":
        return "India"
    if "india" in city.lower():
        return city
    return f"{city}, India"


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through every Happiest Minds posting once and cache India ones.

    _cache_filled is set before the loop so a failure doesn't trigger a
    retry storm on every subsequent keyword call (Honeywell lesson).
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    start = 0
    for page_num in range(_MAX_PAGES):
        if page_num > 0:
            time.sleep(0.15)

        filter_cri = {
            "paginationStartNo": start,
            "selectedCall": "sort",
            "sortCriteria": {"name": "modifiedDate", "isAscending": False},
            "anyOfTheseWords": "",
        }
        files = {
            "filterCri": (None, _json_dumps(filter_cri)),
            "domain": (None, "jobs.happiestminds.com"),
            "companyId": (None, _COMPANY_ID_B64),
        }

        last_exc: Exception | None = None
        r = None
        for attempt in range(3):
            try:
                r = requests.post(_SEARCH_URL, headers=_HEADERS, files=files, timeout=timeout)
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Happiest Minds: 429 rate-limited during cache fill")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Happiest Minds cache fill failed: {exc}") from exc

        if r is None:
            raise RateLimitError(f"Happiest Minds cache fill: no response — {last_exc}")

        payload = r.json().get("data", {})
        batch = payload.get("data", [])
        if not batch:
            break

        for item in batch:
            src = item.get("_source", {})
            country = (src.get("text8") or "").lower()
            if "india" not in country:
                continue
            location = _location_from_job(src)
            if "india" not in location.lower():
                continue
            job_id = src.get("id")
            title = (src.get("jobTitle") or "").strip()
            job_url = src.get("jobUrl") or ""
            if not (job_id and title and job_url):
                continue
            collected.append({
                "id": str(job_id),
                "title": title,
                "location": location,
                "posting_date": _parse_date(src.get("createDate") or ""),
                "application_url": f"{_CAREERS_BASE}/jobview/{job_url}",
            })

        start += len(batch)
        if not payload.get("hasMoreData"):
            break

    _india_cache = collected
    print(f"[Happiest Minds] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Happiest Minds India jobs.

    Keywords are ignored — the shared title/skill filters in matcher.py do
    the real work. The full India pool is cached once (see _fill_cache),
    matching the documented Zwayam-family pattern (CRISIL/Persistent).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single Happiest Minds job.

    Always hits the detail endpoint -- ~32% of postings have no usable
    inline description in the search response (see module docstring).
    """
    job_url = application_url.rsplit("/jobview/", 1)[-1]

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(
                _DETAIL_URL,
                headers={**_HEADERS, "Content-Type": "application/json"},
                json={
                    "jobUrl": job_url,
                    "externalSource": "CAREERSITE",
                    "campusUrl": "empty",
                    "companyId": _COMPANY_ID,
                },
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Happiest Minds description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Happiest Minds description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Happiest Minds description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        return "", ""

    description = _strip_html(data.get("longDescription", ""))
    posting_date = _parse_date(data.get("createDate", "") or "")
    return description, posting_date
