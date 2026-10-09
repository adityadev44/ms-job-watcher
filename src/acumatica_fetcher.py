"""Acumatica India jobs via the verified SmartRecruiters board."""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://api.smartrecruiters.com/v1/companies/Acumatica/postings"
_PUBLIC_BASE = "https://jobs.smartrecruiters.com/Acumatica"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.smartrecruiters.com/Acumatica",
}

# description cache: application_url -> (description, posting_date)
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-06-25T10:11:59.173Z' -> '2026-06-25'."""
    return raw[:10] if raw else ""


def _job_id_from_url(application_url: str) -> str:
    """'https://jobs.smartrecruiters.com/Acumatica/744...-slug' -> '744...'."""
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
                raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"Acumatica search failed after 3 attempts: {exc}") from exc
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
        if country.lower() != "in":
            continue

        city = (loc.get("city") or "").strip()
        if (loc.get('region') or '').upper() == 'TN' and 'tamil nadu' not in city.lower():
            city = f'{city}, Tamil Nadu'
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
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    job_id = _job_id_from_url(application_url)

    for attempt in range(3):
        try:
            r = requests.get(f"{_BASE_URL}/{job_id}", headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on detail for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f'Acumatica description failed after 3 attempts: {exc}') from exc
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
