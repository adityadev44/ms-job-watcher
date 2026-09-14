"""
Ubisoft job fetcher — SmartRecruiters public REST API.

Ubisoft's branded careers site (`www.ubisoft.com/en-us/company/careers`) is
a React SPA that never surfaces its API base URL in the shipped JS bundle
directly (no `smartrecruiters`/`api.` string anywhere in the main chunk).
The real backend was found via a live web search for
"careers.smartrecruiters.com/Ubisoft2" (Ubisoft's own SmartRecruiters
career-site skin, `careers.smartrecruiters.com/Ubisoft2/ubisoft-india-recruitment-drive`)
— same ATS/API shape already integrated for Nagarro/NECSWS/PhonePe (see
necsws_fetcher.py; this file mirrors that pattern).

Company identifier confirmed live: "Ubisoft2" (not the more obvious
"Ubisoft" or "UbisoftIndia" — those return 404/empty).

Search endpoint: GET /v1/companies/Ubisoft2/postings
  - `country=in` is a reliable server-side filter — verified zero leakage
    (all 3 returned postings genuinely `location.country == "in"`).
  - `q` (keyword) does some server-side narrowing but is not authoritative
    (same "loose pre-filter only" caveat as every other SmartRecruiters
    integration in this repo) — the shared matcher's title-family/skill
    checks do the real work.
  - `limit` capped at 100 server-side; pagination via `offset`.

Detail endpoint: GET /v1/companies/Ubisoft2/postings/{postingId}
  - Same `jobAd.sections.{jobDescription,qualifications,additionalInformation}.text`
    shape as NECSWS/Nagarro.

**Current India footprint is 100% Pune** (verified live 2026-09-14: all 3
open India postings — "R&D Engineer (C++)", "R&D Engineer", "Technical
Architect" — are `Pune, MH, India`, part of Ubisoft's "Pune Test studio").
Pune is in this repo's `exclude_locations` list, so 0 current matches is
expected — same "feasible ATS, temporarily/structurally zero matches"
shape as Icertis and BNY Mellon (both onboarded anyway per playbook
precedent) rather than a reason to skip. Ubisoft has 1,000+ India
employees per its own careers-locations page and posts new India reqs
regularly, so a non-Pune opening (or non-testing role) is a real
possibility going forward — worth keeping the pipeline live rather than
declaring infeasible over a single-day snapshot.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings"
_PUBLIC_BASE = "https://jobs.smartrecruiters.com/Ubisoft2"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.smartrecruiters.com/Ubisoft2",
}

_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-08-31T11:35:37.420Z' -> '2026-08-31'."""
    return raw[:10] if raw else ""


def _job_id_from_url(application_url: str) -> str:
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
                raise RateLimitError(f"Ubisoft search failed after 3 attempts: {exc}") from exc
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
