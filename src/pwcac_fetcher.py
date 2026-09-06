"""Fetches PwC Acceleration Centers (India) job listings via Workday CXS.

PwC's careers site (pwc.in/careers -> www.pwc.in/careers/experienced-jobs.html)
embeds a Workday widget (`jobs-25.js`) pointing at
`pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers` -- a single GLOBAL
tenant with ~4,598 open jobs across every PwC territory, not an India-only
board. The standard `locationCountry` facet that most Workday tenants in this
repo use (WID `c4f78be1a8f14da0ab49ce1162348a5e`) is silently IGNORED by this
tenant -- applying it still returns the full 4,598-job global pool (confirmed
live: a job in Cairo, Egypt came back with the facet applied). This tenant
only exposes a flat per-city `locations` facet (347 distinct city values
worldwide, no country grouping), so India is scoped by passing every India
city's location WID explicitly in `appliedFacets.locations` -- same technique
as barclays_fetcher.py's 11 city WIDs / maersk_fetcher.py's site WIDs, just a
larger set (33 WIDs here, spanning Ahmedabad, Bengaluru/Bangalore, Bhopal,
Bhubaneswar, Chennai, Delhi/NCR, Gandhinagar (GIFT City), Gurugram, Hyderabad,
Jaipur, Kolkata, Mumbai, Noida and Pune facility variants -- discovered by
paging the tenant's own `locationMainGroup` facet response and matching
India city name prefixes, 2026-09-06). Querying with exactly these 33 WIDs
and no search text returns `total: 1368`, matching the sum of each WID's own
`count` field exactly -- confirmed precise, not a guess.

`searchText` genuinely narrows server-side within that India-scoped set (a
nonsense token returns 0; adding "senior" to "software engineer" reduces the
count from 602 to 453) -- unlike Deloitte USI/EY GDS's SuccessFactors tenants
in this same onboarding batch, this is real per-keyword filtering, not a
noisy OR full-text match. `pwcac` is therefore NOT registered in
`_IGNORES_KEYWORDS`.

Job locations (`locationsText`, e.g. "Bengaluru Millenia", "Gurugram 10 C",
"Kolkata DN 57") never contain the literal word "India", so ", India" is
appended before handing off to matcher.py's `is_india_job()`. None of the 33
chosen India WIDs correspond to a Tamil Nadu facility (no Coimbatore/Madurai/
Tiruppur entry exists in this tenant's location list), so -- unlike
eurofins_fetcher.py -- no extra Tamil Nadu state-name patch is needed here;
Chennai postings are already covered by config's default `exclude_locations`
city-name match.

Descriptions are not inline -- fetched from the CXS JSON detail endpoint,
same shape as accenture_fetcher.py (`jobPostingInfo.jobDescription` /
`.startDate`), since this is the exact same ATS platform (Workday CXS) just a
different tenant.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://pwc.wd3.myworkdayjobs.com"
_SITE = "Global_Experienced_Careers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/pwc/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/pwc/{_SITE}"

_PAGE_SIZE = 20

# India city location WIDs for the pwc.wd3 tenant's flat `locations` facet.
# Discovered 2026-09-06 by paging the tenant's own `locationMainGroup` facet
# response (347 global city values) and matching India city-name prefixes.
# Sum of each WID's own `count` == 1368, confirmed exact via a live query
# with just these WIDs applied (`total: 1368`) -- not a guess.
_INDIA_LOCATION_WIDS = [
    "e57e6863118d0160ef6ca477342bd0b1",  # Ahmedabad
    "e57e6863118d01f411ec8989342b58c9",  # Ahmedabad
    "eebca0c3bff61020ecbe34c9da530000",  # Airoli
    "e57e6863118d01c410d0a777342bd5b1",  # Bangalore
    "0625c4114f5301af5c3e9d0bdf1c4a05",  # Bangalore (SDC) - Bagmane Tech Park
    "e57e6863118d01c9b91fc587342becc6",  # Bengaluru Brigade Magnum
    "e57e6863118d017a7a40f887342b36c7",  # Bengaluru Millenia
    "a0186028715c01af7a61e2a26c4feaea",  # Bhopal
    "606b09f912311013e7b7a6099f390000",  # Bhubaneswar - Ihub
    "e57e6863118d01c26419ae7a342b2bb6",  # Chennai
    "393a0869abd710143a2beaf60b670000",  # Chennai - Menon Eternity
    "e57e6863118d013f3b2da177342bcbb1",  # Delhi NCR
    "e57e6863118d0143538d8d89342b5dc9",  # Delhi Sucheta
    "f4872aacb3ee012f05fd8850b22c3f40",  # GandhiNagar - GIFT CITY
    "e57e6863118d0159272aef87342b2cc7",  # Gurugram 10 C
    "e57e6863118d013a81f1f387342b31c7",  # Gurugram 8 B
    "d3d4fd4264d2101d8cfff7a82dbe0000",  # Gurugram Downtown 4
    "480ed971333f01ac180c364b7b21c8de",  # Gurugram Novus Tower
    "e57e6863118d01be5ed2aa7a342b26b6",  # Hyderabad
    "e57e6863118d01b854445d88342bb8c7",  # Hyderabad
    "654a31e587e4016effd02a459109a45e",  # Hyderabad - Salarpuria
    "1253f47a256c101dc7c54d7832430000",  # Jaipur - GT Landmark
    "351cca8db89f101a78acb2ea6c5f0000",  # Jaipur - Tonk Road
    "e57e6863118d011c2d21d779342bffb4",  # Kolkata
    "e57e6863118d01def7f26488342bc2c7",  # Kolkata DN 57
    "4b594efa2b6c01cfc90002b5270301c1",  # Kolkata - Magnacon Building
    "e57e6863118d011ca7026188342bbdc7",  # Kolkata Y-14
    "e57e6863118d0101d840b17a342b30b6",  # Mumbai
    "e57e6863118d01e1230f4288342b9ac7",  # Mumbai Goregaon
    "e57e6863118d01c0b8b9ff87342b40c7",  # Mumbai Shivaji Park
    "4286c2c7092210052a2351a32a610000",  # Noida
    "e57e6863118d01cf86b0d379342bfab4",  # Pune
    "e57e6863118d0192617a4988342ba4c7",  # Pune
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
    "Referer": f"{_BASE_URL}/{_SITE}",
}

_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


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


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    global _FIRST_PAGE_IDS

    body = {
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
        # Workday rejects limit > 20 on this tenant with HTTP 400.
        "limit": min(num, 20),
        "offset": start,
        "searchText": keyword,
    }

    r = None
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
                raise RateLimitError("PwC AC Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"PwC AC fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        bullet = p.get("bulletFields", [])
        job_id = bullet[0].strip() if bullet else ""
        if not job_id:
            m = re.search(r"_(\w+WD)(?:-\d+)?$", external_path)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        # locationsText never contains "India" (e.g. "Gurugram 10 C") -- this
        # tenant's India scoping comes entirely from the applied location
        # WIDs, not from the text itself. Append ", India" so matcher.py's
        # is_india_job() passes (same fix as accenture_fetcher.py).
        city = p.get("locationsText", "").strip()
        loc = f"{city}, India" if city else "India"

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and jobs and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []  # wraparound detected

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(3):
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
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("PwC AC description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"PwC AC description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
