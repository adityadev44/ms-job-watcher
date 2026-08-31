"""
PhonePe job fetcher — SmartRecruiters public REST API.

ATS discovery (2026-08-31): PhonePe's old Greenhouse board
(job-boards.greenhouse.io/phonepe, boards-api.greenhouse.io/v1/boards/phonepe)
now 404s — confirmed dead, not a transient outage. The live careers page
(phonepe.com/careers/job-openings/) is a Gatsby SPA with no ATS hint in its
raw HTML/inline scripts (no iframe, no greenhouse/lever/workday/successfactors
string anywhere in app.js). The page-specific chunk
(component---src-pages-careers-job-openings-index-js-*.js) calls PhonePe's
own first-party endpoint https://www.phonepe.com/apollo/job-postings/latest.json
first; that endpoint over-shares (includes NOT_PUBLISHED/INTERNAL/PRIVATE
draft postings alongside PUBLIC ones — a PhonePe-side access-control gap, not
something this fetcher relies on), but every `PUBLIC` row's `applyUrl` points
at jobs.smartrecruiters.com/PHONEPELIMITED/... — confirming PhonePe migrated
to SmartRecruiters. This is the SAME ATS as Nagarro
(careers.smartrecruiters.com/nagarro1) already in this repo, so this fetcher
follows nagarro_fetcher.py's idiom exactly rather than using the leaky
apollo/latest.json endpoint (which isn't a real search API — no pagination,
no keyword/country filtering, and returns unpublished postings not meant to
be public).

Careers site: https://careers.smartrecruiters.com/PHONEPELIMITED
Company identifier confirmed live: "PHONEPELIMITED" (canonical casing
returned in every response's `company.identifier` field and in
`postingUrl`; the API is case-insensitive but this fetcher always sends the
canonical form).

Search endpoint: GET /v1/companies/{companyId}/postings
  - `country` param (ISO-3166 lowercase code, e.g. "in") is a reliable
    server-side filter — verified: `country=us` returns totalFound=0,
    `country=in` returns the same 5/5 postings as an unfiltered query
    (PhonePe's entire current SmartRecruiters board is India-only).
  - `q` (keyword) param does SOME server-side matching but is not a hard
    filter — full-text against description content, not just title: a
    `q=engineer` query matched all 5 postings including "Associate Director
    Product Design - UX", same loose-match behavior already documented for
    Nagarro/SAP Labs. Treated as a pre-filter only; the shared matcher's
    title-family/skill checks do the real work.
  - `limit` is capped at 100 server-side; pagination uses `offset`.

Detail endpoint: GET /v1/companies/{companyId}/postings/{postingId}
  - `jobAd.sections.{jobDescription,qualifications,additionalInformation}.text`
    hold the real job content (HTML). `companyDescription` is generic
    boilerplate identical across postings — excluded to avoid diluting
    skill matches.
  - `postingUrl` (https://jobs.smartrecruiters.com/PHONEPELIMITED/{id}-{slug})
    is the human-facing apply page — only present on the detail response,
    not the search/list response.

Live-verified 2026-08-31: 5 total postings, all India (Bengaluru x4,
Pune x1) — Associate Director Product Design, Engineering Manager Backend,
Product Design Manager, Service Delivery Engineer (Site Reliability), Site
Reliability Engineer 4+ Years. None of these titles currently match
`matching.title_family` (no bare "engineer" title survives — "Engineering
Manager" is an exclude-terms hit, the two SRE titles aren't in the
family list), so 0 alerts is the expected current result — same
"mechanically working, genuinely zero matches today" situation as
ING/eClerx, not a fetcher defect. PhonePe's board is simply tiny right now;
new Software/Backend Engineer postings will be picked up automatically.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_COMPANY_ID = "PHONEPELIMITED"
_BASE_URL = f"https://api.smartrecruiters.com/v1/companies/{_COMPANY_ID}/postings"
_PUBLIC_BASE = f"https://jobs.smartrecruiters.com/{_COMPANY_ID}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.smartrecruiters.com/PHONEPELIMITED",
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
    """'2026-08-28T11:20:45.290Z' -> '2026-08-28'."""
    return raw[:10] if raw else ""


def _job_id_from_url(application_url: str) -> str:
    """'https://jobs.smartrecruiters.com/PHONEPELIMITED/1000...-slug' -> '1000...'."""
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

    r = None
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
                raise RateLimitError(f"PhonePe search failed after 3 attempts: {exc}") from exc
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

    r = None
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
