"""Fetches Alstom job listings via the SAP SuccessFactors J2W HTML search API.

Alstom's ATS is classic SAP SuccessFactors Job2Web hosted at
jobsearch.alstom.com (same "data-row" server-rendered HTML shape as
Nomura/Capgemini -- NOT the newer "Unify" JS theme). Plain GET requests
work: `q=<keyword>&locationsearch=India`, paginated via `startrow=N`
(25 results per page), same pagination shape as Capgemini.

Two things this ATS does differently from Capgemini, discovered live:

1. Keyword filtering is nearly a no-op: every keyword tested (including
   default_keywords like "python developer" or "machine learning engineer")
   returns 210-248 of the ~248-job India-tagged pool. Registered in
   _IGNORES_KEYWORDS so the runner issues one query pass instead of
   repeating the full keyword list.
2. The `locationsearch=India` facet leaks non-India jobs (confirmed:
   "Senior Finance Project Manager" tagged "Perth, IN" -- Perth, Australia,
   not an Indian location -- same data-quality class as the Micron/Lowe's
   Workday facet leakage documented in the playbook). Location strings on
   this ATS never actually contain the word "India" (they end in a bare
   country code, e.g. "Bangalore, KA, IN" or "Ahmedabad, IN"), so blindly
   rewriting every trailing ", IN" to ", India" would also relabel Perth as
   India. Instead: only append ", India" when the city matches a known
   Indian-city whitelist (same "middle ground" fix as lowes_fetcher.py) --
   anything else (Perth, and any other non-India leak) is passed through
   unmodified, so matcher.py's is_india_job() correctly drops it for having
   no "india" substring.

Also normalises Indian state codes (e.g. ", TN, IN") to their full name
before the ", India" append, so Tamil Nadu cities without "Chennai"/
"Madurai" in the name (Coimbatore, in particular) are still caught by
config.yaml's exclude_locations "Tamil Nadu" entry.

Date in search results: "18 Aug 2026"                    -> YYYY-MM-DD
Date on detail pages:   "Tue Aug 18 02:00:00 UTC 2026"    -> YYYY-MM-DD
"""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobsearch.alstom.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # J2W always returns 25 per page

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Indian city tokens seen live on this tenant (and other well-known Indian
# tech/engineering hub cities), used to distinguish real India postings from
# this ATS's occasional non-India leakage under locationsearch=India (see
# module docstring -- "Perth, IN" was observed live).
_INDIA_CITIES = (
    "ahmedabad", "bangalore", "bengaluru", "bhopal", "chandigarh", "chennai",
    "coimbatore", "ghaziabad", "gurgaon", "gurugram", "hyderabad", "indore",
    "jaipur", "kochi", "kolkata", "lucknow", "madhepura", "madurai", "mumbai",
    "nagpur", "delhi", "noida", "pune", "saharanpur", "savli", "sri city",
    "trivandrum", "vadodara", "baroda", "nashik", "kanpur", "vizag",
    "visakhapatnam", "faridabad",
)

# Indian state codes as they appear in "{City}, {Code}, IN" location strings
# on this ATS -- expanded to the full state name so exclude_locations (which
# matches on "Tamil Nadu"/"Kerala" text, not 2-letter codes) still catches
# cities like Coimbatore that don't carry an excluded city name of their own.
_STATE_CODE_TO_NAME = {
    "TN": "Tamil Nadu",
    "KL": "Kerala",
    "MH": "Maharashtra",
    "KA": "Karnataka",
    "GJ": "Gujarat",
    "MP": "Madhya Pradesh",
    "UP": "Uttar Pradesh",
    "HR": "Haryana",
    "TG": "Telangana",
    "AP": "Andhra Pradesh",
    "RJ": "Rajasthan",
    "BR": "Bihar",
    "DL": "Delhi",
}

_STATE_CODE_RE = re.compile(r",\s*([A-Z]{2}),\s*IN$")
_BARE_IN_RE = re.compile(r",\s*IN$")

# Search-result date: "18 Aug 2026" -- also seen as "8 Sept 2026" (4-letter
# September abbreviation), so the month token is 3-4 letters, not fixed at 3.
_SEARCH_DATE_RE = re.compile(r"\d{1,2} [A-Za-z]{3,4} \d{4}")


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Alstom's J2W ATS."""


def _normalize_location(raw: str) -> str:
    """Expand a state code to its full name and append ', India' only for
    recognised India cities -- see module docstring for why a blind
    append is unsafe on this ATS (confirmed Australia leakage)."""
    loc = " ".join(raw.split())
    if not _is_india_city(loc):
        return loc  # left unmodified -> matcher.py's is_india_job() drops it

    m = _STATE_CODE_RE.search(loc)
    if m:
        code = m.group(1)
        state_name = _STATE_CODE_TO_NAME.get(code)
        if state_name:
            loc = _STATE_CODE_RE.sub(f", {state_name}, India", loc)
            return loc
        # Unknown 2-letter code: still a known India city, just append plainly.
        return _BARE_IN_RE.sub(", India", loc)

    if _BARE_IN_RE.search(loc):
        return _BARE_IN_RE.sub(", India", loc)

    return loc


def _is_india_city(loc: str) -> bool:
    low = loc.lower()
    return any(city in low for city in _INDIA_CITIES)


def _parse_search_date(raw: str) -> str:
    """Convert '18 Aug 2026' (search result) to '2026-08-18'."""
    if not raw:
        return ""
    m = _SEARCH_DATE_RE.search(raw)
    if not m:
        return ""
    # Normalise the 4-letter "Sept" abbreviation to the standard 3-letter
    # form so %b parses it (Python's %b only accepts "Sep").
    text = re.sub(r"\bSept\b", "Sep", m.group(0))
    try:
        return datetime.strptime(text, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Tue Aug 18 02:00:00 UTC 2026' (meta tag) to '2026-08-18'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
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
    """Fetch one page of Alstom India jobs for the given keyword.

    ``start`` maps directly to the J2W ``startrow`` query parameter.
    ``num`` is accepted for API compatibility but J2W always returns 25/page.
    """
    params: dict = {
        "q": keyword,
        "locationsearch": "India",
    }
    if start:
        params["startrow"] = start

    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL,
                params=params,
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Alstom J2W: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Alstom fetch failed: {exc}") from exc

    soup = BeautifulSoup(r.text, "html.parser")
    jobs: list[dict] = []

    for row in soup.select("tr.data-row"):
        link = row.select_one("span.jobTitle.hidden-phone a.jobTitle-link")
        if not link:
            link = row.select_one("a.jobTitle-link")
        if not link:
            continue

        href = link.get("href", "").strip()
        title = html_mod.unescape(link.get_text(strip=True))
        if not href or not title:
            continue

        # e.g. "/job/Bangalore-Software-Designer-KA/1397461433/" -> "1397461433"
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id.isdigit():
            continue

        loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
        if not loc_cell:
            loc_cell = row.select_one("span.jobLocation")
        loc_text = ""
        if loc_cell:
            for part in loc_cell.children:
                raw_part = getattr(part, "string", None) or (
                    str(part) if hasattr(part, "strip") else ""
                )
                candidate = raw_part.strip()
                if candidate and not candidate.startswith("+"):
                    loc_text = html_mod.unescape(candidate)
                    break

        loc_text = _normalize_location(loc_text) if loc_text else ""

        date_span = row.select_one("td.colDate.hidden-phone span.jobDate")
        if not date_span:
            date_span = row.select_one("span.jobDate")
        posting_date = _parse_search_date(date_span.get_text(strip=True)) if date_span else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc_text,
            "posting_date": posting_date,
            "application_url": f"{_BASE_URL}{href}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the full job description and posting date from the detail page.

    Returns (description_text, posting_date) where posting_date is YYYY-MM-DD.
    The detail page uses <span itemprop="description" class="jobdescription">
    and <meta itemprop="datePosted" content="Tue Aug 18 02:00:00 UTC 2026">.
    """
    for attempt in range(3):
        try:
            r = requests.get(
                application_url,
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                raise RateLimitError(
                    f"Alstom description: 429 rate-limited for {application_url}"
                )
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    desc_span = soup.select_one("span.jobdescription")
    description = ""
    if desc_span:
        raw = html_mod.unescape(desc_span.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    date_meta = soup.find("meta", {"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_detail_date(date_meta.get("content", ""))

    return description, posting_date
