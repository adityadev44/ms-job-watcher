"""ZF Group job fetcher — SAP SuccessFactors J2W (classic theme) HTML scraping.

ATS identification (Step 1, verified live 2026-09-13): jobs.zf.com is SAP
SuccessFactors J2W classic (same platform/theme family as Nomura/Capgemini/
Dover/YASH already in this repo) -- confirmed via `j2w`/`data-row` markers
in the page source. The public search page is
`https://jobs.zf.com/search/?q={keyword}&locationsearch=India`, fully
server-rendered plain HTML, `?startrow=N` query-string pagination (25/page,
same style as Capgemini -- NOT Nomura's path-based `/9050900/100/` style).

Unlike several other J2W tenants in this repo, ZF's `q=` keyword param
genuinely narrows results server-side (verified: `q=engineer` -> 45 of 70
total India results, `q=finance` -> 9 of 70) -- so this fetcher queries
per-keyword rather than caching the whole pool, mirroring `capgemini_fetcher.py`.

India presence confirmed genuinely NOT Pune-only. ZF's real R&D/software
engineering footprint spans Hyderabad, Bangalore, and Pune roughly evenly
(e.g. "Technical Lead - .NET Core Full stack Developer" and
"Senior Engineer - .NET Core Full stack Developer" in Hyderabad/Bangalore,
"AI/ML Specialist - Agentic AI & Generative AI" in Bangalore) -- Pune is
already covered by `default_exclude_locations`, so no code-level handling
needed beyond passing locations through matcher.py as-is.

Location strings on this tenant are formatted "City, ST, IN, ZIP" (e.g.
"Hyderabad, TG, IN, 500032") rather than the simpler "City, IN" seen at
Nomura -- converted to "City, India" by taking the first comma-separated
token and re-appending ", India" (safer than a blind ", IN" -> ", India"
substring replace, since "IN" also appears as the country-code token itself
mid-string here).

Job-detail pages are plain server-rendered HTML with the description in
`<span class="itemprop="description"" class="jobdescription">` and posting
date in `<meta itemprop="datePosted" content="...">` -- same shape as Nomura,
just a different date format ("Thu Sep 03 00:00:00 UTC 2026").
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.zf.com"
_SEARCH_PATH = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # SuccessFactors J2W classic default page size on this tenant

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _parse_date(raw: str) -> str:
    """Convert 'Thu Sep 03 00:00:00 UTC 2026' to '2026-09-03'."""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


def _normalize_location(raw: str) -> str:
    """"Hyderabad, TG, IN, 500032" -> "Hyderabad, India"; falls back safely."""
    raw = raw.strip()
    if not raw:
        return "India"
    first = raw.split(",")[0].strip()
    if not first or first.upper() == "IN":
        return "India"
    return f"{first}, India"


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
                raise RateLimitError("ZF: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ZF search fetch failed: {exc}") from exc

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

        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id:
            continue

        loc_span = row.select_one("span.jobLocation")
        loc_text = html_mod.unescape(loc_span.get_text(" ", strip=True)) if loc_span else ""
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
                raise RateLimitError(f"ZF detail: 429 on {application_url}")
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
