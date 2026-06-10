"""
S&P Global job fetcher — Workday CXS REST API (spgi.wd5.myworkdayjobs.com).

Plain HTTP POST/GET, no Playwright needed.

No server-side India facet is available on this Workday tenant, so all global
results are returned and India detection is done by normalising known Indian
state names in locationsText ("Noida, Uttarpradesh" → "Noida, India").

Pagination: the full API page is always returned so start advances correctly.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://spgi.wd5.myworkdayjobs.com"
_SITE = "SPGI_Careers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/spgi/{_SITE}/jobs"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/spgi/{_SITE}"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"

_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Indian state names as they appear (lowercased, spaces stripped) in S&P Global's
# Workday locationsText (e.g. "Noida, Uttarpradesh" or "Mumbai, Maharastra").
_INDIA_STATES = {
    "uttarpradesh", "uttarakhand",
    "telangana", "karnataka",
    "maharashtra", "maharastra",
    "tamilnadu",
    "haryana", "delhi",
    "westbengal", "odisha",
    "gujarat", "rajasthan",
    "punjab", "andhrapradesh",
    "kerala", "jharkhand",
    "madhyapradesh",
}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _normalise_location(loc_text: str) -> str:
    """Convert 'Noida, Uttarpradesh' → 'Noida, India'.

    Returns the original string unchanged if it is not recognised as India.
    """
    if "india" in loc_text.lower():
        return loc_text
    parts = loc_text.split(",")
    if len(parts) >= 2:
        state_norm = parts[-1].strip().lower().replace(" ", "")
        if state_norm in _INDIA_STATES:
            return f"{parts[0].strip()}, India"
    return loc_text


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    """Fetch one page of S&P Global jobs matching keyword.

    Returns all jobs in the page (global, not India-filtered); the shared matcher
    applies is_india_job() after location normalisation. Pagination advances by
    the full page length so offsets stay correct.
    """
    body = {
        "appliedFacets": {},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }

    for attempt in range(3):
        try:
            r = requests.post(
                _SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout
            )
            if r.status_code == 429:
                raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(f"S&P Global search failed after 3 attempts: {exc}") from exc
            time.sleep(2 ** attempt)

    postings = r.json().get("jobPostings", [])

    jobs = []
    for j in postings:
        loc_text = j.get("locationsText", "") or ""

        # Skip multi-location entries ("2 Locations" etc.) — can't determine India.
        if re.search(r"\d+\s+Locations?", loc_text, re.IGNORECASE):
            continue

        bullet = j.get("bulletFields", [])
        job_id = str(bullet[0]) if bullet else ""
        if not job_id:
            # Fall back to numeric suffix of externalPath
            ext_path = j.get("externalPath", "")
            m = re.search(r"_(\d+)(?:-\d+)?$", ext_path)
            job_id = m.group(1) if m else ""
        if not job_id:
            continue

        ext_path = j.get("externalPath", "")
        application_url = f"{_JOB_BASE}{ext_path}"
        location_str = _normalise_location(loc_text)

        jobs.append({
            "id": job_id,
            "title": j.get("title", "").strip(),
            "location": location_str,
            "posting_date": "",   # filled by fetch_job_description
            "application_url": application_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch full description and posting date for a single S&P Global job.

    Converts the human-facing URL to the Workday CXS API path.
    Returns (description_text, posting_date_YYYY-MM-DD).
    """
    api_url = application_url.replace(f"{_BASE_URL}/{_SITE}", _DETAIL_BASE)

    for attempt in range(3):
        try:
            r = requests.get(api_url, headers={**_HEADERS, "Content-Type": ""}, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on detail for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    jpi = r.json().get("jobPostingInfo", {})

    posting_date = (jpi.get("startDate") or "")[:10]   # already YYYY-MM-DD
    description = _strip_html(jpi.get("jobDescription") or "")

    return description, posting_date
