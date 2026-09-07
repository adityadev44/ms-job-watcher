"""
ixigo job fetcher — SmartRecruiters public REST API.

ATS discovery (live, 2026-09-07): www.ixigo.com/careers 302-redirects to
careers.ixigo.com, a Next.js site whose own first-party endpoint
(`careers.ixigo.com/api/openings`, `x-powered-by: ixigo`/`x-backend:
career-site-...` response headers confirm it's ixigo's own server, not a
vendor's) serves the job list for that page. This endpoint over-shares the
same "company's own frontend wrapper" shape as PhonePe's
`apollo/job-postings/latest.json` (see phonepe_fetcher.py) — but critically,
clicking an actual job card on the rendered page (confirmed live via a real
Playwright click, capturing the resulting new-tab URL rather than guessing)
opens `https://jobs.smartrecruiters.com/ixigo/{id}` — confirming ixigo's
real underlying ATS is SmartRecruiters, company identifier **"ixigo"**
(lowercase; confirmed as the canonical form used in every `postingUrl`).
This is the SAME ATS as PhonePe/Nagarro already in this repo, so this
fetcher follows phonepe_fetcher.py's idiom exactly rather than using
ixigo's own wrapper endpoint (which has no documented pagination/filtering
contract and mixes internal-only IDs with the public SmartRecruiters ones —
confirmed live that its `jobVacancyId` field for one posting,
`11491196458`, is a distinct alias that resolves correctly against
SmartRecruiters' own detail endpoint but is NOT the same value as
SmartRecruiters' canonical `id`, `744000147427269`, for that identical
posting — extra indirection with no benefit over calling SmartRecruiters
directly).

Careers site: https://careers.smartrecruiters.com/ixigo
Search endpoint: GET /v1/companies/ixigo/postings
  - `country` param (ISO-3166 lowercase, e.g. "in") is a reliable
    server-side filter — verified live: `country=us` returns totalFound=0,
    `country=in` returns the same totalFound=10 as an unfiltered query
    (ixigo's entire current SmartRecruiters board is India-only: Gurugram/
    Gurgaon/New Delhi).
  - `q` (keyword) does SOME server-side matching but is loose/OR-based
    across tokens, same documented behavior as PhonePe/Nagarro's tenants on
    this ATS — e.g. `q=".NET developer"` and `q="C# developer"` both
    return the same 6 of 10 postings (matching on "developer" alone), while
    a genuine nonsense token correctly returns 0 (confirmed live:
    `q=zzznonsense123` -> totalFound=0, so it's a real filter, just an
    imprecise one). Treated as a pre-filter only; the shared matcher's
    title-family/skill checks do the real narrowing.
  - `limit` is capped at 100 server-side; pagination uses `offset`.

Detail endpoint: GET /v1/companies/ixigo/postings/{id}
  - `jobAd.sections.{jobDescription,qualifications,additionalInformation}.text`
    hold the real job content (HTML). `companyDescription` is generic
    boilerplate identical across postings — excluded to avoid diluting
    skill matches (same exclusion as phonepe_fetcher.py).
  - `postingUrl` (https://jobs.smartrecruiters.com/ixigo/{id}-{slug}) is
    the human-facing apply page — only present on the detail response, not
    the search/list response.

Live-verified 2026-09-07: 10 total postings, all India (Gurugram x6,
Gurgaon x2, New Delhi x2) — includes "Senior Software Engineer - Android",
"Senior Devops Engineer", "Senior UI Developer (ReactJS)", "Research
Engineer - Agent Intelligence & Evaluation", "Machine Learning Engineer
(Agent Intelligence & Evaluations)", "Software Engineer - 2 Backend" —
several genuinely match the default keyword list.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_COMPANY_ID = "ixigo"
_BASE_URL = f"https://api.smartrecruiters.com/v1/companies/{_COMPANY_ID}/postings"
_PUBLIC_BASE = f"https://jobs.smartrecruiters.com/{_COMPANY_ID}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.smartrecruiters.com/ixigo",
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
    """'2026-09-04T06:48:55.902Z' -> '2026-09-04'."""
    return raw[:10] if raw else ""


def _job_id_from_url(application_url: str) -> str:
    """'https://jobs.smartrecruiters.com/ixigo/744000147427269-slug' -> '744000147427269'."""
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
                raise RateLimitError(f"ixigo search failed after 3 attempts: {exc}") from exc
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
