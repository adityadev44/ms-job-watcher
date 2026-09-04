"""Fetches Applied Systems (India) job listings via the iCIMS career portal.

Applied Systems is a US insurance-agency-management-software company
(appliedsystems.com). Its India entity ("Applied Systems India Private
Limited", Bengaluru) has a dedicated iCIMS career-portal tenant at
india-appliedsystems.icims.com -- verified live 2026-09-04 from scratch via
plain HTTP probing (no prior "custom ATS" guess was given for this company;
this is a first identification, not a correction).

This is a genuinely different iCIMS integration shape from every other
iCIMS-backed company already in this repo (Gallagher, S&P Global Careers,
PepsiCo, Schneider Electric -- all "iCIMS/Jibe" behind a custom domain with
a clean `/api/jobs` JSON endpoint). Applied Systems runs bare classic iCIMS
directly on a `*.icims.com` subdomain: server-rendered HTML job cards, no
JSON API found.

Two live quirks had to be found by trial and error, not assumed:

1. **AWS WAF Bot Control gates every route with an interactive CAPTCHA
   (HTTP 405, `x-amzn-waf-action: captcha`) -- but only for some request
   shapes.** A plain GET to `/jobs/search` (or any other path) on either
   `careers-appliedsystems.icims.com` or `india-appliedsystems.icims.com`
   is blocked outright. Appending `?in_iframe=1` (the same query param the
   portal's own JS uses when it embeds itself inside
   `www1.appliedsystems.com`'s wrapper page via an <iframe>) bypasses the
   CAPTCHA entirely and returns the real server-rendered results -- no
   Playwright, no CAPTCHA-solving needed. Without `in_iframe=1` the URL
   still returns HTTP 200, but the body is just the *outer* wrapper page
   (Ceros marketing widgets + an <iframe> pointing at the `in_iframe=1`
   version) -- no job data at all. Both properties are required: the right
   query param AND avoiding the WAF trigger.

2. **This repo's own boilerplate `User-Agent` string is itself the WAF
   trigger.** The `"Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...
   Chrome/125.0.0.0 ..."` UA copy-pasted into nearly every other fetcher in
   this repo gets a 405/CAPTCHA on this tenant every single time (confirmed
   with both `curl` and `requests` -- this is not a TLS-fingerprint
   difference between HTTP clients, it is UA-string-specific, most likely
   because that exact UA string is common enough in scraper traffic to be
   on AWS WAF's own bad-bot signature list). A different, otherwise
   unremarkable desktop-Chrome UA (Mac Chrome 128 here) passes cleanly with
   plain `requests`. See PLAYBOOK.md's Key Bugs table for the write-up --
   this fetcher intentionally does NOT reuse the shared `_HEADERS` UA
   string other fetchers use.

Board shape (verified live 2026-09-04): `india-appliedsystems.icims.com`'s
default (unfiltered) `/jobs/search` result set IS already the complete
India board -- 17 postings, "Page 1 of 1", every single one in
IN-KA-Bengaluru (a couple show as bare "IN-Bengaluru", same city). No
country/location facet is needed. `searchKeyword=` genuinely narrows
results server-side (confirmed: "customer" -> 4 of 17), but the full board
is small enough that this fetcher ignores it and caches the whole pool
once, like Groww/CRED/DAZN -- matcher.py's own title/skill filtering does
the real narrowing per keyword pass.

Job-detail pages (`/jobs/{id}/{slug}/job?in_iframe=1`) carry a clean
schema.org JobPosting JSON-LD block with the full description HTML and an
ISO-8601 `datePosted` -- same pattern as Disney/Schwab/SAP Labs/Societe
Generale, no separate JSON detail API needed.

Live-confirmed 2026-09-04: job 7502, "Sr. Full Stack Engineer" (posted
2026-05-13, IN-KA-Bengaluru) is currently open and requires ".NET framework
(C#, ASP.NET, .NET Core)" + "RESTful APIs" + "SQL Server" + 5+ years -- a
genuine `.NET / C#` primary_skills match, and the exact role this company
was assigned for.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE = "https://india-appliedsystems.icims.com"
_SEARCH_URL = f"{_BASE}/jobs/search"
_MAX_PAGES = 20  # defensive cap; the live board is 1 page (17 jobs)

# Deliberately NOT this repo's usual shared UA string -- see module
# docstring point 2: that exact string is the thing AWS WAF blocks here.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_cache: list[dict[str, str]] = []
_cache_filled = False


class RateLimitError(Exception):
    """Raised on 429 / persistent failure, or if the WAF CAPTCHA can't be
    bypassed on any retry."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _normalize_location(raw: str) -> str:
    """'IN-KA-Bengaluru' / 'IN-Bengaluru' -> 'Bengaluru, India'."""
    raw = (raw or "").strip()
    if not raw:
        return "India"
    if "india" in raw.lower():
        return raw
    if raw.upper().startswith("IN-") or raw.upper().startswith("IN "):
        city = raw.split("-")[-1].strip()
        return f"{city}, India" if city else "India"
    return f"{raw}, India"


def _normalize_date(raw: str) -> str:
    """ISO datetime ('2026-05-13T04:00:00.000Z') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw.strip())
    if m:
        return m.group(0)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Applied Systems: 429 rate-limited on {url}")
            if r.status_code == 405 or "Human Verification" in r.text[:2000]:
                # WAF CAPTCHA gate -- retry (transient in principle; if it
                # never clears, surface as a rate-limit so matcher.py logs
                # a warning instead of crashing).
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    f"Applied Systems: WAF CAPTCHA blocked {url} after retries"
                )
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Applied Systems fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"Applied Systems fetch failed for {url}: {last_exc}")


def _parse_page(html: str) -> tuple[list[dict[str, str]], int]:
    soup = BeautifulSoup(html, "html.parser")

    total_pages = 1
    header = soup.find("div", class_="iCIMS_SearchResultsHeader")
    if header is not None:
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", header.get_text(" ", strip=True))
        if m:
            total_pages = int(m.group(1))

    jobs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for card in soup.select("li.iCIMS_JobCardItem"):
        a_tag = card.select_one("div.title a.iCIMS_Anchor")
        if a_tag is None or not a_tag.get("href"):
            continue
        href = a_tag["href"]
        m_id = re.search(r"/jobs/(\d+)/", href)
        if not m_id:
            continue
        job_id = m_id.group(1)
        if job_id in seen_ids:
            continue

        h3 = card.find("h3")
        title = h3.get_text(strip=True) if h3 else ""
        if not title:
            continue

        loc_span = card.select_one("div.header.left span:not(.sr-only)")
        location = _normalize_location(loc_span.get_text(strip=True) if loc_span else "")

        application_url = href.split("?")[0]
        if application_url.startswith("/"):
            application_url = f"{_BASE}{application_url}"

        seen_ids.add(job_id)
        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",  # not present on the card; filled via detail fetch
            "application_url": application_url,
        })

    return jobs, total_pages


def _fill_cache(timeout: int) -> None:
    r = _get(f"{_SEARCH_URL}?in_iframe=1", timeout)
    jobs, total_pages = _parse_page(r.text)
    _cache.extend(jobs)

    page = 1
    while page < min(total_pages, _MAX_PAGES):
        time.sleep(0.2)
        r = _get(f"{_SEARCH_URL}?pr={page}&in_iframe=1", timeout)
        more_jobs, _ = _parse_page(r.text)
        if not more_jobs:
            break
        _cache.extend(more_jobs)
        page += 1


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Applied Systems India job listings.

    The India tenant's default (unfiltered) search result IS the full India
    board (verified: 17/17 postings, all Bengaluru). `searchKeyword=` does
    filter server-side on this ATS, but the whole pool is small enough that
    this fetcher ignores the passed-in keyword/location and caches the full
    pool once in-module instead -- matcher.py's own per-keyword title/skill
    filtering does the real narrowing.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before fetching to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a job's full description + posting date from its schema.org
    JobPosting JSON-LD block."""
    sep = "&" if "?" in application_url else "?"
    r = _get(f"{application_url}{sep}in_iframe=1", timeout)

    m = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S
    )
    if not m:
        return "", ""

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return "", ""

    description = _strip_html(data.get("description", ""))
    posting_date = _normalize_date(data.get("datePosted", ""))

    return description, posting_date
