"""Fetches Volvo Group (trucks/buses/construction equipment -- DISTINCT from
Volvo Cars, a separately-owned company with its own ATS) job listings via
the SAP SuccessFactors J2W classic HTML search API.

ATS discovery (live, 2026-09-13): jobs.volvogroup.com is the SAME classic
J2W "data-row" template already onboarded for Nomura/Capgemini/ZF (not the
newer "Unify" theme that needs the `/services/recruiting/v1/jobs` REST call
-- confirmed by finding real `<tr class="data-row">` rows server-rendered
in the plain HTML response, no JS execution needed). `q=`/`locationsearch=`
GET params both genuinely narrow server-side (confirmed live: `q=software
engineer&locationsearch=India` narrows the full 41-job India pool to 29;
`startrow=25` pagination works exactly like Capgemini's).

India presence confirmed genuinely NOT excluded-city-only: of 41 India
postings at investigation time, locations are Bangalore/Hosakote (both
Karnataka, not excluded) and Mumbai -- no Chennai/Pune/Tamil
Nadu/Chandigarh/Kochi/Kerala/Trivandrum/Lucknow/Nagpur/Madurai/Kolkata/
Indore observed at all. Real, on-target titles seen: "AI / ML Engineer",
"Senior Software Engineer", "Senior Software Engineer - Medallia
Developer", "Software Engineer (HLS)", "Software Engineer - Uipath", "HR
Software Engineer".

Location format: "Bangalore, IN, 562122" -> normalised to "Bangalore,
India" (state/postal code dropped) so matcher.py's is_india_job() and
exclude_locations checks work the same way as Capgemini's fetcher.

Search-result rows carry no date column for this tenant (unlike
Capgemini's "Jun 26, 2026" `span.jobDate`) -- posting_date is populated
only from the detail page's `datePosted` meta tag.
"""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.volvogroup.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # classic J2W's default page size for this tenant

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Volvo Group's J2W tenant."""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Wed Sep 02 02:00:00 UTC 2026' (meta tag) to '2026-09-02'."""
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
    """Fetch one page of Volvo Group India jobs for the given keyword.

    ``start`` maps directly to the J2W ``startrow`` query parameter.
    ``num`` is accepted for API compatibility but J2W returns a fixed
    number of rows per page for this tenant.
    """
    params: dict = {
        "q": keyword,
        "locationsearch": "India",
    }
    if start:
        params["startrow"] = start

    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Volvo Group J2W: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Volvo Group fetch failed: {exc}") from exc

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

        # Job ID: trailing numeric segment of the path
        # e.g. "/job/Bangalore-Specialist-.../1361604755/" -> "1361604755"
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id.isdigit():
            continue

        loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
        if not loc_cell:
            loc_cell = row.select_one("span.jobLocation")
        loc_text = ""
        if loc_cell:
            for part in loc_cell.children:
                raw_part = getattr(part, "string", None) or (str(part) if hasattr(part, "strip") else "")
                candidate = raw_part.strip()
                if candidate and not candidate.startswith("+"):
                    loc_text = html_mod.unescape(candidate)
                    break

        # Normalise "Bangalore, IN, 562122" -> "Bangalore, India"
        loc_text = re.sub(r",\s*IN\b.*$", ", India", loc_text)
        if not loc_text:
            loc_text = "India"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc_text,
            "posting_date": "",  # not present in search rows; filled from detail page
            "application_url": f"{_BASE_URL}{href}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the full job description and posting date from the detail page.

    Detail page uses <span class="jobdescription"> and
    <meta itemprop="datePosted" content="Wed Sep 02 02:00:00 UTC 2026">.
    """
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Volvo Group description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
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
