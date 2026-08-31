"""Fetches SAP Labs (jobs.sap.com) job listings via the SAP SuccessFactors J2W
HTML search API -- the same classic (non-Unify) J2W platform as Nomura and
Capgemini, confirmed live: view-source on /search/ shows real server-rendered
`<tr class="data-row">` rows (no CSRF/REST API dance needed like the Unify
tenants at Wipro/HCLTech/Standard Chartered).

jobs.sap.com/search/ accepts plain GET requests: `q=<keyword>` IS honoured
server-side (a nonsense keyword returns 0 rows; "sales"/"finance"/"dot net"
each return small, different counts) and `locationsearch=india` restricts to
genuine India postings server-side (verified: all returned locations are
Bangalore/Mumbai/Gurgaon/Pune/New Delhi, none leaked from another country --
unlike the broken facets seen on Micron/Verizon/Lowe's). Pagination is
`startrow=N`, 25 results per page -- identical shape to Capgemini, not
Nomura's path-based `/9050900/100/` scheme.

Job detail pages are server-rendered plain HTML with the same anchors as
Nomura/Capgemini: `span.jobdescription` for the JD body and
`meta[itemprop="datePosted"]` (format "Mon Aug 24 00:00:00 UTC 2026") for the
posting date. `meta[itemprop="hiringOrganization"]` = "SAP" confirms the
`company=SAP` SuccessFactors tenant code mentioned in the task brief (visible
in the site's own login redirect to career5.successfactors.eu with
`company=SAP`); the public-facing site itself needs no such param.

Total India-job pool on this public career site is small at any snapshot
(~50 total across ALL departments when this was written) but heavily skewed
toward engineering/AI titles -- SAP Labs India (Bangalore) is one of SAP's
largest R&D centres, so nearly every open India req is software/AI/ML-
flavoured. Location strings never say the word "India" (they say "IN"), so
the fetcher normalises "Bangalore, IN, 560066" -> "Bangalore, India, 560066"
the same way Nomura/Capgemini do.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.sap.com"
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


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from SAP's J2W tenant."""


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_detail_date(raw: str) -> str:
    """Convert 'Mon Aug 24 00:00:00 UTC 2026' (meta tag) to '2026-08-24'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
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
    """Fetch one page of SAP Labs India jobs for the given keyword.

    ``start`` maps directly to the J2W ``startrow`` query parameter.
    ``num`` is accepted for API compatibility but J2W always returns 25 per page.
    """
    params: dict = {
        "q": keyword,
        "locationsearch": "india",
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
                raise RateLimitError("SAP Labs J2W: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"SAP Labs fetch failed: {exc}") from exc

    soup = BeautifulSoup(r.text, "html.parser")
    jobs: list[dict] = []

    for row in soup.select("tr.data-row"):
        # Title + href -- prefer the hidden-phone span to avoid duplicate mobile text
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
        # e.g. "/job/Bangalore-Senior-Staff-AI-Engineer/1392118733/" -> "1392118733"
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id.isdigit():
            continue

        # Location -- prefer the non-mobile column cell
        loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
        if not loc_cell:
            loc_cell = row.select_one("span.jobLocation")
        loc_text = html_mod.unescape(loc_cell.get_text(strip=True)) if loc_cell else ""

        # Normalise "Bangalore, IN, 560066" -> "Bangalore, India, 560066"
        # (SAP's location strings use the "IN" country code, never the word
        # "India", so is_india_job() would otherwise reject every result.)
        loc_text = re.sub(r",\s*IN\b", ", India", loc_text)
        if not loc_text:
            loc_text = "India"

        # No date column in the search listing on this tenant (unlike
        # Capgemini's colDate) -- posting_date is filled by
        # fetch_job_description from the detail page's datePosted meta tag.
        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc_text,
            "posting_date": "",
            "application_url": f"{_BASE_URL}{href}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the full job description and posting date from the detail page.

    Returns (description_text, posting_date) where posting_date is YYYY-MM-DD.
    The detail page uses <span class="jobdescription"> and
    <meta itemprop="datePosted" content="Mon Aug 24 00:00:00 UTC 2026">.
    """
    for attempt in range(3):
        try:
            r = requests.get(
                application_url,
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                raise RateLimitError(f"SAP Labs description: 429 rate-limited for {application_url}")
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
