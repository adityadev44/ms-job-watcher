"""Fetches AIG (American International Group) job listings via the Workday
public REST API.

AIG's ATS is Workday, hosted at aig.wd1.myworkdayjobs.com, site "aig"
(confirmed via robots.txt: the tenant only publishes three sites — "aig",
"early_careers", and "japan"; "aig" is the one linked from aig.com/home/
careers and aig.com/home/careers/experienced-professionals). Same CXS REST
pattern as Wells Fargo/Citi/Northern Trust — plain POST, no browser needed.

No India locationCountry facet exists on this tenant right now. A full
enumeration of the "locationCountry" (30 entries) and "locations" (139
site-address entries) facets returned by an unfiltered query — done live
during onboarding on 2026-08-31 — contains zero India-labelled entries of
any kind (no Gurugram/Bengaluru/Mumbai/Pune/Hyderabad/Chennai, no "India"
country row). This does not mean AIG has no India presence — the company
has a real ~4,000-person GCC in Gurugram plus smaller offices in Mumbai/
Bengaluru/Hyderabad/Pune/Chennai (confirmed independently via web search,
unrelated to the ~26%-AIG-owned Tata AIG joint venture, a different legal
entity with its own separate hiring) — it means this public global req
board simply has no open India-tagged requisition at the moment. Fetches
globally per keyword (searchText genuinely narrows results server-side,
confirmed by differing totals per keyword) and filters India client-side,
same shape as Genpact/WTW/Hexaware.

IMPORTANT location-parsing trap: this tenant's US entries use the state
abbreviation as a location prefix — e.g. "IN-Jeffersonville" and
"Remote-IN" both mean Indiana, not India. Do NOT treat an "IN-" prefix or
bare "IN" token as an India signal. Only a whole "india" word-boundary
match or one of a small set of known real AIG India office city names is
trusted (see `_looks_like_india`).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://aig.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/aig/aig/jobs"
_JOB_BASE = f"{_BASE_URL}/aig"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/aig/aig"
_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/aig",
}

# Real AIG India office cities (Gurugram GCC + smaller sites), used to
# recognise a bare single-city locationsText value ("Sumida-ku", "Brussels"
# style — no country suffix) as India. Deliberately does NOT include any
# "IN-" prefix check — see module docstring re: Indiana collision.
_INDIA_CITY_TOKENS = (
    "gurugram",
    "gurgaon",
    "bengaluru",
    "bangalore",
    "mumbai",
    "pune",
    "hyderabad",
    "chennai",
    "ahmedabad",
    "thiruvananthapuram",
    "trivandrum",
)
_INDIA_WORD_RE = re.compile(r"\bindia\b", re.IGNORECASE)


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper — Workday returns relative strings like "Posted 3 Days Ago"
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


def _looks_like_india(raw_loc: str) -> str | None:
    """Return a normalised India location string, or None if not India.

    "N Locations" placeholder text is never trusted (we cannot tell which
    country without a detail fetch, and no current AIG posting needs it).
    """
    if not raw_loc:
        return None
    low = raw_loc.lower()
    if _INDIA_WORD_RE.search(low):
        return raw_loc
    if "location" in low:  # "2 Locations", "7 Locations", etc.
        return None
    if any(city in low for city in _INDIA_CITY_TOKENS):
        return f"{raw_loc}, India"
    return None


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
    body = {
        "appliedFacets": {},
        "limit": num,
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
                raise RateLimitError("AIG Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"AIG fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        # Job ID from bulletFields (e.g. ["JR2603238", "AIG Europe S.A. ..."])
        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(JR\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            m = re.search(r"_(JR\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        raw_loc = p.get("locationsText", "").strip()
        loc = _looks_like_india(raw_loc)
        if loc is None:
            continue

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
    """
    if application_url.startswith(_JOB_BASE):
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/aig/", 1)[-1]
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
                raise RateLimitError("AIG description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"AIG description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
