"""Fetches OYO job listings via the SmartRecruiters public REST API.

ATS: SmartRecruiters
Confirmed live 2026-09-26 via careers.smartrecruiters.com/OYO1, which renders
"Careers at OYO" with SmartRecruiters branding. The canonical company
identifier in every API response's `company.identifier` field is "OYO1".

Search endpoint:
    GET https://api.smartrecruiters.com/v1/companies/OYO1/postings
    - `country` param (ISO-3166 lowercase, "in") is a reliable server-side
      filter (same behavior as Nagarro/PhonePe on the same platform).
    - `q` (keyword) does SOME server-side pre-filtering but is not a hard
      filter — full-text against description content, not just titles.
      Treated as a loose pre-filter; the shared matcher's title-family/skill
      checks do the real work.
    - `limit` is capped at 100 server-side; pagination uses `offset`.

Detail endpoint:
    GET https://api.smartrecruiters.com/v1/companies/OYO1/postings/{id}
    - `jobAd.sections.{jobDescription,qualifications,additionalInformation}.text`
      hold the real job content (HTML). `companyDescription` is generic
      boilerplate across all postings — excluded to avoid diluting skill
      matches.
    - `postingUrl` (https://jobs.smartrecruiters.com/OYO1/{id}-{slug}) is the
      human-facing apply page — present only in the detail response.

India detection: the SmartRecruiters `location.country` field returns "in" for
India postings reliably. `location.city` gives a bare city name (e.g.
"Gurugram") with no "India" substring, so ", India" is appended client-side
for recognised India cities. The country filter is authoritative; the
city normalisation is a defense-in-depth measure.

Status 2026-09-26: board confirmed live, 0 open positions (OYO has scaled
back hiring significantly; the board previously had postings for Gurugram,
Bengaluru, and Hyderabad engineering offices). This is a "feasible ATS,
temporarily zero matches" situation — same shape as Icertis/BNY Mellon/
PhonePe, not a fetcher defect.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_COMPANY_ID = "OYO1"
_BASE_URL = f"https://api.smartrecruiters.com/v1/companies/{_COMPANY_ID}/postings"
_PUBLIC_BASE = f"https://jobs.smartrecruiters.com/{_COMPANY_ID}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"https://careers.smartrecruiters.com/{_COMPANY_ID}",
}

# description cache: application_url -> (description, posting_date)
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-09-26T10:11:59.173Z' -> '2026-09-26'."""
    return raw[:10] if raw else ""


def _job_id_from_url(application_url: str) -> str:
    """'https://jobs.smartrecruiters.com/OYO1/744...-slug' -> '744...'."""
    tail = application_url.rstrip("/").split(f"{_PUBLIC_BASE}/")[-1]
    return tail.split("-")[0]


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of OYO India jobs via SmartRecruiters.

    keyword is passed as a loose `q` pre-filter to the SmartRecruiters API
    (full-text match, not just title). country=in is the reliable server-side
    India filter. The shared matcher's title-family/skill checks do the real
    narrowing regardless of the keyword pre-filter result.
    """
    params = {
        "q": keyword,
        "country": "in",
        "limit": min(num, 100),
        "offset": start,
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_BASE_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"OYO: 429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(
                    f"OYO search failed after 3 attempts: {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    data = r.json()
    raw_jobs = data.get("content", [])

    jobs = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        if not job_id:
            continue

        loc = j.get("location", {}) or {}
        country = (loc.get("country") or "").strip()
        if country and country.lower() != "in":
            continue

        city = (loc.get("city") or "").strip()
        location_str = f"{city}, India" if city and city.lower() != "india" else "India"

        title = (j.get("name") or "").strip()
        posting_date = _parse_date(j.get("releasedDate", ""))
        application_url = f"{_PUBLIC_BASE}/{job_id}"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single OYO job.

    Fetches from the SmartRecruiters detail endpoint. Results are cached
    by application_url so repeated calls within the same process are free.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    job_id = _job_id_from_url(application_url)

    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                f"{_BASE_URL}/{job_id}", headers=_HEADERS, timeout=timeout
            )
            if r.status_code == 429:
                raise RateLimitError(f"OYO: 429 on detail for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    detail = r.json()
    sections = (detail.get("jobAd") or {}).get("sections") or {}

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
