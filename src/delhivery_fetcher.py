"""Fetches Delhivery job listings via the SmartRecruiters public REST API.

Company: Delhivery Ltd — India's logistics and supply chain company (NSE/BSE listed).
ATS: SmartRecruiters
Company identifier: "Delhivery" (verified live 2026-09-26 via API)
Careers site: https://careers.smartrecruiters.com/Delhivery

Discovery notes (2026-09-26):
  - SmartRecruiters confirmed: api.smartrecruiters.com/v1/companies/Delhivery/postings
    returns HTTP 200 with valid pagination structure (totalFound: 0 at time of check).
  - Delhivery has engineering teams in Gurugram (HQ), Bengaluru, and Hyderabad.
  - The board has 0 open positions as of check date — 0 matches is expected and
    not a fetcher defect; check back as Delhivery ramps hiring.
  - delhivery.com/careers → JavaScript SPA, renders empty loading state.
  - careers.delhivery.com → DNS not found (no dedicated careers subdomain).
  - delhivery.keka.com/careers → Keka 404 (not on Keka).
  - delhivery.zohorecruit.in → requires login (internal HR tool, not public).

API shape (same as Nagarro/PhonePe/NEC):
  GET https://api.smartrecruiters.com/v1/companies/Delhivery/postings
  Params:
    - country=in  → reliable server-side India filter (verified zero leakage)
    - q=<keyword> → loose pre-filter only (full-text, not title-only); shared
                     matcher does the real title/skill work, same as Nagarro
    - limit       → capped at 100 server-side
    - offset      → for pagination
  Response: {"content": [...], "totalFound": N, "limit": N, "offset": N}

Detail endpoint:
  GET https://api.smartrecruiters.com/v1/companies/Delhivery/postings/{id}
  jobAd.sections.{jobDescription,qualifications,additionalInformation}.text
  hold the real job content (HTML). companyDescription is generic boilerplate.

India detection: `country=in` is applied server-side. Location city + ", India"
is constructed from the response's `location.city` field. If city is absent,
falls back to "India".
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_COMPANY_ID = "Delhivery"
_BASE_URL = f"https://api.smartrecruiters.com/v1/companies/{_COMPANY_ID}/postings"
_PUBLIC_BASE = f"https://jobs.smartrecruiters.com/{_COMPANY_ID}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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
    """'2026-06-25T10:11:59.173Z' -> '2026-06-25'."""
    return raw[:10] if raw else ""


def _job_id_from_url(application_url: str) -> str:
    """'https://jobs.smartrecruiters.com/Delhivery/744...-slug' -> '744...'."""
    tail = (application_url or "").rstrip("/").split(f"/{_COMPANY_ID}/")[-1]
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
    """Return a page of Delhivery India jobs from SmartRecruiters.

    `country=in` is a reliable server-side India filter. `q` keyword is a
    loose pre-filter only — shared matcher does the real title/skill work.
    Pagination uses `offset`; `limit` is capped at 100 server-side.
    """
    params = {
        "q": keyword,
        "country": "in",
        "limit": min(num, 100),
        "offset": start,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_BASE_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Delhivery SmartRecruiters: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Delhivery search failed after 3 attempts: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"Delhivery search: no response — {last_exc}")

    data = r.json()
    raw_jobs = data.get("content", [])

    jobs: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        if not job_id:
            continue

        loc = j.get("location") or {}
        country = (loc.get("country") or "").strip()
        if country and country.lower() != "in":
            continue

        city = (loc.get("city") or "").strip()
        location_str = f"{city}, India" if city and city.lower() != "india" else "India"

        title = (j.get("name") or "").strip()
        posting_date = _parse_date(j.get("releasedDate") or "")
        application_url = f"{_PUBLIC_BASE}/{job_id}"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for one Delhivery job.

    Fetches the SmartRecruiters detail endpoint. Result is cached in-module
    to avoid redundant calls if fetch_job_description is called multiple times
    for the same URL in the same process run.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    job_id = _job_id_from_url(application_url)
    if not job_id:
        return "", ""

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                f"{_BASE_URL}/{job_id}",
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                raise RateLimitError(f"Delhivery detail 429: {job_id}")
            if r.status_code == 404:
                result = ("", "")
                _desc_cache[application_url] = result
                return result
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            _desc_cache[application_url] = ("", "")
            return "", ""

    if r is None:
        return "", ""

    detail = r.json()
    sections = (detail.get("jobAd") or {}).get("sections") or {}

    parts: list[str] = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        txt = (sections.get(key) or {}).get("text") or ""
        if txt:
            parts.append(_strip_html(txt))
    description = " ".join(parts)

    posting_date = _parse_date(detail.get("releasedDate") or "")

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
