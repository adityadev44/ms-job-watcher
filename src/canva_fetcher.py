"""
Canva job fetcher — SmartRecruiters public REST API.

Canva's branded careers site (`lifeatcanva.com`) sits behind a genuine
Cloudflare managed challenge — a plain `requests.get` on `/en/jobs/`
returns HTTP 403 with `cf-mitigated: challenge` regardless of headers sent
(confirmed live 2026-09-06). The underlying ATS is reachable directly
though: Canva's public apply links resolve to `jobs.smartrecruiters.com/
Canva/...`, and SmartRecruiters' own public REST API (a different host,
no Cloudflare wall) works with a plain unauthenticated GET:

    GET https://api.smartrecruiters.com/v1/companies/Canva/postings

Confirmed live (2026-09-06): HTTP 200, 263 total postings globally.

Verified via direct A/B requests against the live API:
- `country=in` is a reliable server-side filter — 6 of 263 global postings
  are India, all with `location.country == "in"`.
- `q` (keyword) genuinely narrows server-side (0 results for a nonsense
  token, 1/6 for "engineer" scoped to India) — NOT registered in
  `_IGNORES_KEYWORDS`. Note: `keyword=` (a plausible-looking alternate
  param name) is silently ignored — only `q=` actually filters; this
  fetcher uses `q=`.
- `limit` caps at 100 server-side (undocumented, matches every other
  SmartRecruiters tenant already in this repo — Eurofins/Nagarro/NECSWS);
  `offset` paginates cleanly with no wraparound observed. Immaterial here
  since the India pool (6) is far under one page.

Location: `location.country` is a lowercase ISO code ("in"), and
`location.city`/`location.region` are usable directly (e.g. "Bengaluru",
"Delhi", "Vijayawada, ANDHRA PRADESH"). Built the same way as every other
SmartRecruiters fetcher in this repo (Eurofins pattern): `<city>, India`,
falling back to bare "India" if city is blank.

Live-verified 2026-09-06 totals: 6 of 263 global postings are India
(Bengaluru x4, Delhi x1, Vijayawada x1) — current titles are "Product
Support Specialist, Education Team", "Project Manager, Education Team"
(x2), "Content Marketing Specialist", "AI Quality Evaluator - Hindi", and
"Product Localisation Lead - India". Zero matches against the standard
keyword list today (0/6) — a genuine current fact, not a fetcher defect:
Canva's small India board right now is entirely
localisation/education/content roles, no title containing "software
engineer"/"AI engineer"/"python developer"/etc. verbatim (including "AI
Quality Evaluator - Hindi", which despite mentioning AI is a data-labeling
role, not an engineering title, and doesn't match `title_family` either).

Job descriptions are NOT inline in the search response (Eurofins/Nagarro
pattern) — fetched from the same SmartRecruiters detail endpoint
(`GET /v1/companies/Canva/postings/{id}`). The real JD content lives in
`jobAd.sections.jobDescription`/`qualifications`/`additionalInformation`;
all three are concatenated since which one holds substantive text varies
by posting (same reasoning as every other SmartRecruiters fetcher here).
`companyDescription` is generic Canva boilerplate, excluded.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://api.smartrecruiters.com/v1/companies/Canva/postings"
_PUBLIC_BASE = "https://jobs.smartrecruiters.com/Canva"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.lifeatcanva.com/en/jobs/",
}

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


def _build_location(loc: dict) -> str:
    city = (loc.get("city") or "").strip()
    region = (loc.get("region") or "").strip()
    if city and region and region.lower() not in city.lower():
        return f"{city}, {region}, India"
    if city:
        return f"{city}, India"
    return "India"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Canva's India job postings.

    India is scoped server-side via `country=in` (verified reliable — no
    non-India leakage observed across the full pool). `keyword` genuinely
    narrows results server-side via SmartRecruiters' `q` param; `location`
    is not sent — matches every other single-country-facet fetcher in this
    repo (Eurofins/Nagarro/NECSWS).
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
                raise RateLimitError("Canva search: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Canva search failed: {exc}") from exc

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Canva search returned non-JSON body: {exc}") from exc

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
    `additionalInformation` sections (which one holds the real JD content
    varies by posting on this tenant); `companyDescription` is generic
    Canva boilerplate, excluded.
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
                raise RateLimitError(f"Canva description: 429 rate-limited for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            raise RateLimitError(f"Canva description fetch failed: {exc}") from exc

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
