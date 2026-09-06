"""
Freshworks job fetcher — SmartRecruiters public REST API.

Careers site: https://www.freshworks.com/company/careers/
SmartRecruiters portal: https://careers.smartrecruiters.com/Freshworks
Company identifier confirmed live: "Freshworks"
(same SmartRecruiters platform family as Nagarro/SopraSteria/Eurofins already
in this repo).

Search endpoint: GET /v1/companies/Freshworks/postings
  - `country=in` is a reliable server-side India filter — confirmed live.
  - `q` (keyword) param is a genuine server-side filter — 0 results for a
    nonsense token; targeted results for technology keywords.
  - `limit` capped at 100 server-side. Pagination via `offset`.

Detail endpoint: GET /v1/companies/Freshworks/postings/<id>
  - `jobAd.sections.{jobDescription,qualifications,additionalInformation}.text`
    hold the HTML job content.
  - `postingUrl` is the human-facing apply URL on jobs.smartrecruiters.com.

Live-verified 2026-09-05: 48 total India postings.
Keywords work server-side (0 for nonsense).
Freshworks India offices: Chennai, Bengaluru.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""

_COMPANY_ID = "Freshworks"
_BASE_URL = f"https://api.smartrecruiters.com/v1/companies/{_COMPANY_ID}/postings"
_PUBLIC_BASE = f"https://jobs.smartrecruiters.com/{_COMPANY_ID}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.smartrecruiters.com/Freshworks",
}

# Description cache: application_url -> (description, posting_date)
_desc_cache: dict[str, tuple[str, str]] = {}

# Pagination-wraparound guard (see fetcher contract)
_FIRST_PAGE_IDS: set[str] | None = None


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-06-25T10:11:59.173Z' -> '2026-06-25'."""
    return raw[:10] if raw else ""


def _job_id_from_url(application_url: str) -> str:
    """Extract SmartRecruiters numeric job ID from a public URL."""
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
    global _FIRST_PAGE_IDS

    params = {
        "q": keyword,
        "country": "in",
        "limit": min(num, 100),
        "offset": start,
    }

    for attempt in range(3):
        try:
            r = requests.get(_BASE_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Freshworks: 429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(
                    f"Freshworks search failed after 3 attempts: {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    data = r.json()
    raw_jobs = data.get("content", [])

    jobs: list[dict] = []
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

    # Pagination-wraparound guard
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description from SmartRecruiters posting detail endpoint."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    job_id = _job_id_from_url(application_url)

    for attempt in range(3):
        try:
            r = requests.get(f"{_BASE_URL}/{job_id}", headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Freshworks: 429 on detail for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    detail = r.json()
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
