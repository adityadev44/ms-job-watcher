"""
Eurofins job fetcher — SmartRecruiters public REST API.

Eurofins Scientific is a large, decentralized global bioanalytical/testing-
laboratory company (`eurofins.com`) with dozens of country/business-unit
career microsites. The India-relevant *software* careers presence lives at
`careers.eurofins.com/in/` (a custom marketing front-end), but that page is
just a skin: live DevTools/network investigation (2026-09-04) found its own
job-list page (`/in/jobs?...`) fetches ALL jobs client-side from a hidden
`#apiJobsURL` value pointing at a legacy custom aggregator
(`atsintegration.eurofins.com/ATSWebService.asmx/GetJobs`), which itself
proxies the REAL ATS. Every job's own `applyUrl` in that aggregator's
response — and the "Featured Jobs" links directly on the marketing page —
point at `jobs.smartrecruiters.com/Eurofins/...`, confirming SmartRecruiters
is the actual backing ATS (same platform family already in this repo via
Nagarro/NECSWS/PhonePe). This fetcher talks to SmartRecruiters' public REST
API directly rather than the aggregator, because it returns real state/region
codes (needed for Tamil Nadu exclusion — see below) and a full job-detail
endpoint with structured description sections, neither of which the
aggregator provides (its "description" field is a ~60-char title/ref-code
teaser only, not the real JD).

Company identifier confirmed live: "Eurofins" (`api.smartrecruiters.com/v1/
companies/Eurofins/postings`), matching the public
`jobs.smartrecruiters.com/Eurofins/<id>-<slug>` apply links.

Verified via direct A/B requests against the live API (2026-09-04):
- `country=in` is a reliable server-side filter — 332 total India postings
  across every department (IT/EITSI ~215, QA ~49, Testing & Lab ~20,
  Marketing ~19, Finance ~13, etc.) — the India-relevant *software* subset
  is entirely under the `IT` department, part of Eurofins IT Solutions
  India Pvt Ltd (EITSI), a ~3000-person dedicated Global Software Delivery
  Center per the company's own site — a genuine product/engineering
  presence, not a lab-technician-only board.
- `q` (keyword) genuinely narrows server-side (0 results for a nonsense
  token, 103/332 for ".NET", 332/332 for an empty query) — NOT registered
  in `_IGNORES_KEYWORDS`.
- `limit` caps at 100 server-side (same as Nagarro/NECSWS); `offset`
  paginates cleanly to the true total with no wraparound observed.
- Titles are unusually strong signal for a company this size: of 215 IT
  postings, 63 pass the shared `title_family` check, and 48 of those (76%)
  have a genuine `.NET / C#` primary-skill hit in the real description —
  this precision is already high without any extra filtering, so
  `require_tech_in_description` is deliberately NOT enabled here (see the
  onboarding report for the full reasoning).
- **New title_family precision gap found, not fixed (same "flag, don't
  silently patch" discipline as every other such gap in this repo):** many
  real, unambiguous .NET IT titles here use "Fullstack" as one word (not
  hyphenated) — e.g. "Dot Net Fullstack Developer (Angular)", ".Net
  Fullstack Engineer", "Principal .Net Fullstack Developer" — which never
  matches `title_family`'s "full stack engineer"/"full-stack developer"
  phrases (both assume a space, and `_normalize_text` only turns hyphens
  into spaces, not concatenated words). The same shop's "Principal
  .Net Engineer"/"Lead Developer (.Net + Angular)" level-banded titles also
  carry no "software"/"backend"/"application" qualifier word `title_family`
  currently requires. ~90 real .NET-titled IT postings are lost to this gap
  in the current pool — a genuinely new instance of the leveling/naming
  precision backlog already flagged for CitiusTech/DAZN/Novartis/Alegeus,
  not touched here.
- No real `AI / ML / Python`-track match currently exists: only 3 IT titles
  mention AI at all ("AI Content Support Analyst", "AI Enablement Lead,
  EITSI", "AI Software Architect") and none matches any `title_family`
  phrase ("analyst"/"lead"/"architect" alone are not covered) — a genuine
  current zero for that track, not a fetcher defect.

Location handling — a genuinely new leak class, not previously in this
repo's Key Bugs table:
- SmartRecruiters' `location.region` field IS a real Indian state code here
  (`KA`, `HR`, `TN`, `MH`, `TS`, ...), unlike most Workday tenants in this
  repo where no usable state field exists at all. Chennai/Chandigarh/Kolkata
  postings are already caught by config's default `exclude_locations` city
  names, but **Coimbatore (13 jobs) and Tiruppur (6 jobs) — both genuinely
  Tamil Nadu cities — carry `region: "TN"` but a city string that never
  says "Tamil Nadu" or "Chennai"**, so they would silently leak past the
  shared exclude list on city-name text alone. Fixed here at the fetcher
  level (not a config change) by appending ", Tamil Nadu" whenever
  `region == "TN"`, so the existing default `exclude_locations` "Tamil
  Nadu" entry catches them without any shared-file edit.
- One current non-IT posting ("Technical Sales Manager") carries a joint
  `city: "Mumbai or Pune"` value — handled defensively with a
  `_pick_display_location` segment-picker (same pattern as
  energyexemplar_fetcher.py's semicolon-joint-posting fix) so a real
  non-excluded city offered alongside an excluded one isn't dropped
  entirely; currently moot for real matches (that title fails
  `title_family`) but kept as defense-in-depth against a future IT posting
  using the same joint-city convention.

Job descriptions are NOT inline in the search response (Wells
Fargo/Nagarro pattern) — fetched from the same SmartRecruiters detail
endpoint (`GET /v1/companies/Eurofins/postings/{id}`). Unlike Nagarro,
which real content lives in depends on the posting: some jobs put the
actual JD text in `jobAd.sections.jobDescription`, others put it in
`qualifications` instead (observed directly, not assumed) — both are
concatenated (along with `additionalInformation`) so neither posting shape
silently loses its content; `companyDescription` is generic Eurofins
boilerplate and excluded, same reasoning as every other SmartRecruiters
fetcher here.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://api.smartrecruiters.com/v1/companies/Eurofins/postings"
_PUBLIC_BASE = "https://jobs.smartrecruiters.com/Eurofins"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.eurofins.com/in/",
}

# Region codes on this tenant are real Indian state abbreviations. Only the
# ones whose full name is needed to trip config's default `exclude_locations`
# substring check (city name alone doesn't say it) are mapped — currently
# just Tamil Nadu (Coimbatore/Tiruppur). See module docstring.
_EXCLUDE_STATE_BY_REGION = {"TN": "Tamil Nadu"}

# Mirrors config.yaml's `_defaults.exclude_locations` anchor. Duplicated here
# (not imported) because fetchers are self-contained modules with no access
# to per-run config — same reasoning as energyexemplar_fetcher.py, used only
# to pick a sane segment out of a joint multi-city posting before handing
# off to matcher.py, which applies the real config-driven exclusion.
_EXCLUDED_CITY_TOKENS = [
    "chennai", "tamil nadu", "pune", "chandigarh", "kochi", "kerala",
    "trivandrum", "lucknow", "nagpur", "madurai", "kolkata", "indore",
]

_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from SmartRecruiters."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-09-03T17:35:09.460Z' -> '2026-09-03'."""
    return raw[:10] if raw else ""


def _pick_city_segment(raw_city: str) -> str:
    """Reduce a joint multi-city string ("Mumbai or Pune") to one segment.

    Prefers the first segment that mentions none of the standard excluded-
    city tokens, so a job jointly posted in a valid city and an excluded one
    isn't dropped purely because the excluded city is *also* listed (same
    reasoning as energyexemplar_fetcher.py's `_pick_display_location`). If
    every segment is excluded (or there's only one segment), the original
    string is returned unchanged.
    """
    segments = [s.strip() for s in re.split(r"\s+or\s+|[;/]", raw_city) if s.strip()]
    if len(segments) < 2:
        return raw_city
    for seg in segments:
        if not any(tok in seg.lower() for tok in _EXCLUDED_CITY_TOKENS):
            return seg
    return raw_city


def _build_location(loc: dict) -> str:
    """Build the location string handed to matcher.py from SmartRecruiters'
    `location` object, appending the real state name for regions whose city
    text alone wouldn't trip config's default `exclude_locations` (Tamil
    Nadu — see module docstring)."""
    city = (loc.get("city") or "").strip()
    region = (loc.get("region") or "").strip().upper()

    if city:
        city = _pick_city_segment(city)

    state = _EXCLUDE_STATE_BY_REGION.get(region, "")

    if not city or city.lower() == "india":
        return f"{state}, India" if state else "India"
    if state and state.lower() not in city.lower():
        return f"{city}, {state}, India"
    return f"{city}, India"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Eurofins's India job postings.

    India is scoped server-side via `country=in` (verified reliable —
    zero non-India leakage across the full pool). `keyword` genuinely
    narrows results server-side; `location` is not sent — matches every
    other single-country-facet fetcher in this repo.
    """
    params = {
        "q": keyword,
        "country": "in",
        "limit": min(num, 100),
        "offset": start,
    }

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_BASE_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Eurofins search: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Eurofins search failed: {exc}") from exc

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Eurofins search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for j in payload.get("content", []):
        job_id = str(j.get("id") or "").strip()
        title = (j.get("name") or "").strip()
        if not job_id or not title:
            continue

        loc = j.get("location", {}) or {}
        country = (loc.get("country") or "").strip()
        if country and country.lower() != "in":
            continue

        location_str = _build_location(loc)
        if "india" not in location_str.lower():
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": _parse_date(j.get("releasedDate", "")),
            "application_url": f"{_PUBLIC_BASE}/{job_id}",
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date via the SmartRecruiters detail API.

    Concatenates `jobDescription`, `qualifications`, and
    `additionalInformation` sections — which one holds the real JD content
    varies by posting on this tenant (verified directly, not assumed), so
    all three are joined rather than picking one. `companyDescription` is
    generic Eurofins boilerplate, excluded.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    job_id = application_url.rstrip("/").split(f"{_PUBLIC_BASE}/")[-1].split("-")[0]

    r = None
    for attempt in range(3):
        try:
            r = requests.get(f"{_BASE_URL}/{job_id}", headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Eurofins description: 429 rate-limited for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            raise RateLimitError(f"Eurofins description fetch failed: {exc}") from exc

    try:
        detail = r.json()
    except ValueError:
        return "", ""

    sections = detail.get("jobAd", {}).get("sections", {}) or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        txt = (sections.get(key) or {}).get("text", "")
        if txt:
            parts.append(_strip_html(txt))
    description = " ".join(parts)

    posting_date = _parse_date(detail.get("releasedDate", ""))

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
