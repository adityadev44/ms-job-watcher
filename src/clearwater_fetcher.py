"""Fetches Clearwater Analytics job listings via the Workday public REST API.

Clearwater Analytics' ATS is Workday, hosted at
clearwateranalytics.wd1.myworkdayjobs.com (tenant "clearwateranalytics",
site "Clearwater_Analytics_Careers"). Confirmed live 2026-09-08:
POST to /wday/cxs/clearwateranalytics/Clearwater_Analytics_Careers/jobs
returns real jobPostings.

Unlike most other Workday tenants in this repo, this tenant exposes no
country-level facet at all (no `locationCountry` / `Location_Country` /
`Country_and_Jurisdiction` key) -- the only location facet is `locations`,
a flat list of named offices. India is scoped by hardcoding the WIDs of the
three India offices observed in that facet:

    Office - Bengaluru  01d55a00bbf71001b8b8823b5f8a0000  (3 jobs)
    Office - Mumbai     d43550e826551001b850bf5675ae0000  (14 jobs)
    Office - Noida      71c813351f2701c8975f348fc4005405  (35 jobs)

No Pune/Chennai/Tamil Nadu/Kochi/Chandigarh office exists on this tenant, so
no exclude-location leakage is currently possible from the office facet
itself -- exclusion still runs centrally in matcher.py as a safety net if
that ever changes. `locationsText` never contains the word "India" (it's
just "Office - Bengaluru" etc.), so ", India" is appended client-side for
matcher.py's location check to work.

This tenant also caps `limit` at 20 -- requesting more raises HTTP 400 (same
quirk as Nvidia/Walmart/Shell in this repo). searchText genuinely narrows
results server-side (confirmed: "software engineer" cuts the India pool from
51 to 15), but since the India pool is already small, keyword is ignored and
the full India office pool is fetched per page; matcher.py's own title/skill
filters do the real narrowing.

The detail endpoint's startDate is already ISO format (YYYY-MM-DD), so no
relative-date parsing is needed for descriptions; the search response's own
`postedOn` field is a relative string ("Posted 3 Days Ago") and is converted
client-side, same as Wells Fargo/Citi.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://clearwateranalytics.wd1.myworkdayjobs.com"
_SITE = "Clearwater_Analytics_Careers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/clearwateranalytics/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/clearwateranalytics/{_SITE}"

# Workday hard-caps this tenant's page size at 20 -- a higher limit returns
# HTTP 400 (verified live 2026-09-08).
_PAGE_SIZE = 20

# India office WIDs from the tenant's own `locations` facet (verified live
# 2026-09-08). No country-level facet exists on this tenant.
_INDIA_LOCATION_WIDS = [
    "01d55a00bbf71001b8b8823b5f8a0000",  # Office - Bengaluru
    "d43550e826551001b850bf5675ae0000",  # Office - Mumbai
    "71c813351f2701c8975f348fc4005405",  # Office - Noida
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": _JOB_BASE,
}

# Word-boundary guard: never let a location containing "Indiana"-style
# substrings pass as India (playbook's PayPal/FactSet/Invesco bug class).
_INDIA_WORD_RE = re.compile(r"\bindia\b", re.IGNORECASE)


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper -- Workday returns relative strings like "Posted 3 Days Ago"
# ---------------------------------------------------------------------------

def _parse_posted_on(posted_on: str) -> str:
    """Convert Workday's relative date string to YYYY-MM-DD."""
    if not posted_on:
        return ""
    s = posted_on.strip().lower()
    today = date.today()

    if "today" in s:
        return today.strftime("%Y-%m-%d")
    if "yesterday" in s:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if "30+" in s:
        return (today - timedelta(days=30)).strftime("%Y-%m-%d")

    m = re.search(r"(\d+)\s+day", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+week", s)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+month", s)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")

    return ""


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    # Tenant caps limit at 20; keyword is ignored -- see module docstring.
    body = {
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
        "limit": min(num, _PAGE_SIZE),
        "offset": start,
        "searchText": "",
    }

    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    json=body,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Clearwater Analytics Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Clearwater Analytics fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        # locationsText is just "Office - Bengaluru" / "2 Locations" -- never
        # contains "India" on this tenant. Since every result here is already
        # scoped to an India office via the locations facet, append ", India"
        # for matcher.py's is_india_job() check. Word-boundary guard kept as
        # a safety net in case a non-India office name is ever mistakenly
        # added to _INDIA_LOCATION_WIDS.
        if not _INDIA_WORD_RE.search(loc):
            loc = f"{loc}, India" if loc else "India"

        external_path = p.get("externalPath", "")
        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date).
    The startDate field in the detail response is already ISO format
    (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    api_url,
                    headers=_HEADERS,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                raise RateLimitError("Clearwater Analytics description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Clearwater Analytics description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
