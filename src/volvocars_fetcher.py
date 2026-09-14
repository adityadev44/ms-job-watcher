"""Volvo Cars job fetcher -- SAP SuccessFactors J2W (classic theme) HTML scraping.

Note: Volvo Cars (jobs.volvocars.com) is a distinct company from Volvo
Group/Volvo Trucks (a separate legal entity with its own careers site,
onboarded independently elsewhere in this repo) -- this fetcher is
passenger-vehicle Volvo Cars only.

ATS identification (Step 1, verified live 2026-09-13): jobs.volvocars.com
is SAP SuccessFactors J2W classic (same platform/theme family as
Nomura/Capgemini/Dover/YASH/ZF already in this repo) -- confirmed via
`j2w`/`data-row` markers and jQuery/SuccessFactors script includes in the
page source. The public search page is
`https://jobs.volvocars.com/search/?q={keyword}&locationsearch=India`,
fully server-rendered plain HTML, `?startrow=N` query-string pagination
(20/page, same style as ZF/Capgemini).

India footprint is small but real: at investigation time, exactly ONE
current India posting exists tenant-wide -- "Sr Software Engineer - Tools
& Insight" in Bengaluru (Information Technology/Digital department,
posted 2026-08-16). This is Bengaluru, not Pune -- clears the "Pune-only
GCC" exclusion precedent trivially since there's no Pune presence to worry
about; the single real posting is itself a genuine SDE hire.

Quirk found live and defended against: `locationsearch=India` combined
with a `q=` keyword that matches ZERO India postings silently falls back
to an unfiltered GLOBAL result page instead of returning zero rows (e.g.
`q=finance&locationsearch=India` returned 20 global rows including a
"Ridgeville, SC" -- South Carolina -- posting). Confirmed this does NOT
happen when the keyword has a genuine India match (`q=engineer` and
`q=software` each correctly return only the one real Bengaluru posting).
To stay safe regardless of which case fires, every row is defensively
re-checked client-side for a `, IN,` country-code token in its own
`jobLocation` text before being kept -- non-India fallback rows are
discarded even if the server mis-scoped the request.

Location strings are formatted "City, ST, IN, ZIP" (e.g. "Bengaluru, KA,
IN, 562122"), same shape as ZF -- normalized to "City, India".

Job-detail pages are plain server-rendered HTML with the description in
`<span class="jobdescription">` and posting date in
`<meta itemprop="datePosted" content="...">` (format "Sun Aug 16
02:00:00 UTC 2026") -- same shape as ZF/Nomura.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.volvocars.com"
_SEARCH_PATH = f"{_BASE_URL}/search/"
_PAGE_SIZE = 20  # SuccessFactors J2W classic default page size on this tenant

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_COUNTRY_TOKEN_RE = re.compile(r",\s*IN\s*,", re.IGNORECASE)

_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _parse_date(raw: str) -> str:
    """Convert 'Sun Aug 16 02:00:00 UTC 2026' to '2026-08-16'."""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


def _normalize_location(raw: str) -> str:
    """"Bengaluru, KA, IN, 562122" -> "Bengaluru, India"; falls back safely."""
    raw = raw.strip()
    if not raw:
        return "India"
    first = raw.split(",")[0].strip()
    if not first or first.upper() == "IN":
        return "India"
    return f"{first}, India"


def _is_india_row(raw_location: str) -> bool:
    """Defends against this tenant's zero-India-match fallback bug (see
    module docstring) -- only trust rows whose own location text carries
    a real ", IN," country-code token, regardless of what locationsearch
    was requested."""
    return bool(_COUNTRY_TOKEN_RE.search(raw_location or ""))


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    params = {
        "q": keyword or "",
        "locationsearch": "India",
    }
    if start:
        params["startrow"] = start

    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_PATH, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Volvo Cars: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Volvo Cars search fetch failed: {exc}") from exc

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tr.data-row")

    jobs: list[dict] = []
    for row in rows:
        link = row.select_one("a.jobTitle-link")
        if not link:
            continue
        href = link.get("href", "")
        title = html_mod.unescape(link.get_text(strip=True))
        if not href or not title:
            continue

        loc_span = row.select_one("span.jobLocation")
        loc_text = html_mod.unescape(loc_span.get_text(" ", strip=True)) if loc_span else ""
        if not _is_india_row(loc_text):
            continue  # server fell back to a non-India global result set

        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id:
            continue

        location_str = _normalize_location(loc_text)
        app_url = href if href.startswith("http") else f"{_BASE_URL}{href}"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": "",  # populated by fetch_job_description
            "application_url": app_url,
        })

    return jobs[:num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Volvo Cars detail: 429 on {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException:
            if attempt < 2:
                time.sleep(1)
                continue
            _desc_cache[application_url] = ("", "")
            return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    desc_el = soup.select_one("span.jobdescription") or soup.select_one('[itemprop="description"]')
    description = html_mod.unescape(desc_el.get_text(" ", strip=True)) if desc_el else ""

    posting_date = ""
    date_meta = soup.find(attrs={"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_date(date_meta.get("content", ""))

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
