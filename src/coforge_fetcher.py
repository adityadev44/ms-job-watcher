"""
Coforge job fetcher — Zwayam ATS.

Coforge (formerly NIIT Technologies) markets `www.coforge.com/careers`, which
301-redirects to `careers.coforge.com/coforge/` — an Angular SPA (runtime/
polyfills/main webpack chunks, `<base href="/coforge/">`, no jobs in the
server-rendered HTML). Live inspection of the bundled `main.*.js` (2026-09-05)
found the SPA's own `./projects/coforge/src/environments/environment.ts`
module compiled in verbatim:

    const environment = {
      COMPANYID: "MTUxNzM=",              // base64 -> "15173"
      COMPANYURL: "Y2FyZWVycy5jb2ZvcmdlLmNvbS8jIS8vbWFuYWdl",
      DOMAIN: "careers.coforge.com",
      APIENDPOINT: "https://public.zwayam.com/",
      APIENDPOINTNEW: "https://public.zwayam.com/",
      JOBSHAREURL: "https://public.zwayam.com",
      TENANTAPIURL: "https://public.zwayam.com"
    };

This confirms the real backing ATS is **Zwayam** — the same shared
multi-tenant platform already onboarded twice in this repo (CRISIL, company
ID 15438; Persistent, its own subdomain tenant). Coforge is a third Zwayam
tenant, using the shared `public.zwayam.com` endpoint like CRISIL (not a
company-specific subdomain like Persistent's `apipersistent.zwayam.com`).
Also visible in the same bundle: unrelated leftover example tenant URLs from
Zwayam's own platform vendor, "openings.co" (e.g.
`flipkart.preprod1.openings.co`, `trianzdigital.preprod1.openings.co`) —
confirms Zwayam/openings.co is a shared white-label platform serving many
unrelated companies, consistent with the CRISIL/Persistent findings already
in this repo.

Verified live against the real API (2026-09-05):
- `POST /jobs/search` (multipart form: `filterCri` JSON blob +
  `domain`="careers.coforge.com" + `companyId`=base64 "MTUxNzM=") returns
  Coforge's *global* job pool — 113 total postings across India, USA/Canada,
  UK, Australia, and Mexico (there is no country-facet request parameter;
  filtering is done client-side here, same as CRISIL). Of those, 70 are
  genuinely India-located (confirmed per-job via the structured
  `jobLocationRecord` list's own `country` field — every one of the 113 jobs
  carries a fully populated, internally-consistent `jobLocationRecord`, no
  fallback to a free-text field was ever needed, unlike CRISIL where that
  structured list is sometimes absent).
- `anyOfTheseWords` genuinely narrows results server-side here (e.g.
  "engineer" -> 22/113, ".net" -> 6/113, "python" -> 7/113, a nonsense token
  -> 0/113) — same as CRISIL, unlike Persistent. Deliberately NOT used
  anyway: the whole pool is small enough (113 jobs total, 70 India) to cache
  once per process and let matcher.py's title/skill filters do the real
  work, avoiding any keyword-matching surprises — same reasoning as
  crisil_fetcher.py. Server-enforced page size is a fixed 10
  (`facetedSearchConfig.paginationHowMuch`), confirmed live, not configurable
  via any request parameter.
- Full JD text lives behind a separate detail endpoint,
  `POST /jobs-service/v1/jobs/careersite` with
  `{jobUrl, externalSource: "CAREERSITE", campusUrl: "empty", companyId}`
  (companyId here is the *decoded* "15173", not the base64 form used in the
  search request — identical two-form-of-the-same-ID quirk as CRISIL).
  `longDescription` carries real, rich HTML-formatted JD content (verified on
  multiple postings, including an AI-track one — see below) — not a stub or
  boilerplate field.
- Posting dates: `createDate` on both the search result and the detail
  response, format `DD-Mon-YYYY` (e.g. "18-Feb-2026") — same format as
  CRISIL's Zwayam tenant.
- Job detail page URL pattern confirmed live (HTTP 200):
  `https://careers.coforge.com/coforge/jobview/{jobUrl}`.

**New location-shape found on this tenant, not previously in this repo's Key
Bugs table (distinct from CRISIL's free-text-with-embedded-joint-cities
shape and from Eurofins/EnergyExemplar's semicolon-joined single string):**
a single Coforge posting open across multiple India cities is represented as
*multiple separate entries* in the structured `jobLocationRecord` list (each
with its own city/state/formattedLocation), not as one joint free-text
string. E.g. job 910072 ("Consultant - ServiceNow ITOM") carries three
records: Greater Noida, Pune, and Hyderabad. Naively joining every record's
`formattedLocation` into one string would reintroduce the exact same
"Pune/Chennai/etc. also offered -> whole job wrongly excluded" bug already
fixed for CRISIL/Eurofins/EnergyExemplar, so `_pick_location()` here instead
*selects* the first record that mentions none of config's default
`exclude_locations` tokens (falling back to the first record verbatim if
every offered city is excluded, so a genuinely all-excluded posting is still
correctly dropped). Confirmed this matters for real, currently-open data:
job 891276 ("Python GEN AI" — see below) is jointly posted in Pune + Noida;
naive joining would have permanently excluded it via the "Pune" substring
even though "Noida" (not excluded — only "Nagpur" is) is also on offer.

Two separate fields on this tenant disagree with each other and are NOT used
for matching: `locationSeparatedbySlash` and `location` both frequently show
a *different* city than the job's own real `jobLocationRecord` (e.g. job
927570 shows `locationSeparatedbySlash: "Bengaluru"`, `location:
"Hyderabad"`, while its only `jobLocationRecord` entry is genuinely
Hyderabad) — these two fields appear to reflect an internal
recruiter/office-assignment view rather than the posting's actual work
location, so `jobLocationRecord` is trusted exclusively here, same
precedence CRISIL already established as a fallback (here it's the primary
and only source, since it's always present).

Real title/skill signal on this pool (this is a staffing/GCC-delivery
company, not a product company, so titles skew toward insurance-platform
delivery roles — Guidewire, Duckcreek — for named clients like SEI, Union
Bank of India, Goldman Sachs Services, Secura Insurance, Sammons Financial):
one confirmed real `AI / ML / Python` hit — job 891276, "Python GEN AI"
(Pune/Noida), whose real JD explicitly names "Retrieval Augmented
Generation", "Pinecone", and "Python" among other AI/ML terms. **New
title_family precision gap found, not fixed (same "flag, don't silently
patch" discipline as every other such gap in this repo):** the title itself,
"Python GEN AI", matches no `title_family` phrase (no "developer"/
"engineer"/"lead"/"architect" qualifier at all) — a genuinely new title
*shape* distinct from every prior flagged instance (PepsiCo/Nutanix's
"manager", Luxoft's "principal", Resideo's non-adjacent "Backend ...
Engineer", Omnissa's "Member Technical Staff 3"). This costs a real,
currently-open, on-topic AI/ML match. No `.NET / C#` track hit currently
exists in the live India pool (the India-relevant tech titles here are
Java/Guidewire/Duckcreek/SAP/ServiceNow-flavored; no posting's JD names
.NET/C#/ASP.NET/Entity Framework) — a genuine current zero for that track,
not a fetcher defect.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime
from json import dumps as _json_dumps

import requests

_CAREERS_BASE = "https://careers.coforge.com/coforge"
_API_BASE = "https://public.zwayam.com"
_SEARCH_URL = f"{_API_BASE}/jobs/search"
_DETAIL_URL = f"{_API_BASE}/jobs-service/v1/jobs/careersite"
_COMPANY_ID_B64 = "MTUxNzM="  # base64("15173")
_COMPANY_ID = "15173"
_MAX_PAGES = 60  # safety ceiling; server page size is a fixed 10 (~113 total jobs today)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_CAREERS_BASE}/",
    "Origin": "https://careers.coforge.com",
}

# Mirrors config.yaml's `_defaults.exclude_locations` anchor. Duplicated here
# (not imported) because fetchers are self-contained modules with no access
# to per-run config — same reasoning as eurofins_fetcher.py/
# energyexemplar_fetcher.py, used only to pick a sane single record out of a
# multi-city posting before handing location off to the shared matcher,
# which applies the real config-driven exclusion itself.
_EXCLUDED_CITY_TOKENS = [
    "chennai", "tamil nadu", "pune", "chandigarh", "kochi", "kerala",
    "trivandrum", "lucknow", "nagpur", "madurai", "kolkata", "indore",
]

_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from the Zwayam API."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-Mon-YYYY' (e.g. '18-Feb-2026') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _pick_location(records: list[dict]) -> str:
    """Return one representative location string for matcher.py.

    Coforge represents a job open across multiple India cities as several
    separate `jobLocationRecord` entries (not one joint free-text string like
    CRISIL/Eurofins/EnergyExemplar) — prefers the first record whose
    `formattedLocation` mentions none of the standard excluded-city tokens,
    so a genuine multi-city posting that also happens to include an excluded
    city (e.g. Pune) isn't dropped purely because that city is *also*
    offered. If every record is excluded, the first record is returned
    unchanged so the job is still correctly excluded.
    """
    if not records:
        return ""
    for rec in records:
        floc = (rec.get("formattedLocation") or "").strip()
        if floc and not any(tok in floc.lower() for tok in _EXCLUDED_CITY_TOKENS):
            return floc
    return (records[0].get("formattedLocation") or "").strip()


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through Coforge's global job pool once and cache India ones.

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
            "domain": (None, "careers.coforge.com"),
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
                    raise RateLimitError("Coforge: 429 rate-limited during cache fill")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Coforge cache fill failed: {exc}") from exc

        if r is None:
            raise RateLimitError(f"Coforge cache fill: no response — {last_exc}")

        payload = r.json().get("data", {})
        batch = payload.get("data", [])
        if not batch:
            break

        for item in batch:
            src = item.get("_source", {})
            records = src.get("jobLocationRecord") or []
            countries = {(rec.get("country") or "").strip() for rec in records}
            if countries != {"India"}:
                continue

            job_id = src.get("id")
            title = (src.get("jobTitle") or "").strip()
            job_url = src.get("jobUrl") or ""
            if not (job_id and title and job_url):
                continue

            location = _pick_location(records)
            if "india" not in location.lower():
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
    print(f"[Coforge] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Coforge's India jobs.

    Keywords are ignored — although `anyOfTheseWords` genuinely narrows
    results server-side on this tenant (verified live), the shared
    title/skill filters in matcher.py do the real work; the full India pool
    (~70 jobs, well under a single cache-once fetch) is cached in-module
    instead. See module docstring.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Coforge job."""
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
                raise RateLimitError(f"Coforge description: 429 rate-limited for {job_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Coforge description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Coforge description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        return "", ""

    description = _strip_html(data.get("longDescription", ""))
    posting_date = _parse_date(data.get("createDate", "") or "")
    return description, posting_date
