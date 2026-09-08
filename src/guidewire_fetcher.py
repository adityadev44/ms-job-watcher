"""Fetches Guidewire Software job listings via the Workday public REST API.

Guidewire's ATS is Workday, tenant `guidewire`, site `External`, hosted at
guidewire.wd5.myworkdayjobs.com. The public careers page at
www.guidewire.com/about/careers/jobs embeds `wd5.myworkdaysite.com` job links
(a CNAME'd public-facing front end for the same tenant); the CXS JSON API
lives on the `guidewire.wd5.myworkdayjobs.com` host directly and needs no
browser.

India is filtered server-side via the `locationCountry` facet. The facet
WID (`c4f78be1a8f14da0ab49ce1162348a5e`) is the same cross-tenant GUID
reused across Fidelity/Wells Fargo/Citi/Northern Trust/MUFG/Shell — verified
live on 2026-09-08: 44 of 141 total jobs, all genuinely in Bangalore.

Like most Workday tenants in this repo, `limit` caps at 20 (>20 returns
HTTP 400) and `total` reports 0 on paginated (offset > 0) requests — the
empty `jobPostings` array is the real termination signal (Accenture/PepsiCo
pattern), not `total`.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_TENANT_HOST = "https://guidewire.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_TENANT_HOST}/wday/cxs/guidewire/External/jobs"
_DETAIL_BASE = f"{_TENANT_HOST}/wday/cxs/guidewire/External"

# Public-facing job page host embedded on www.guidewire.com/about/careers/jobs
_PUBLIC_JOB_BASE = "https://wd5.myworkdaysite.com/recruiting/guidewire/external"

_PAGE_SIZE = 20

# India locationCountry WID for Guidewire's Workday tenant — the same
# cross-tenant GUID used by several other tenants in this repo. Verified
# 2026-09-08: 44 India results (all Bangalore) out of 141 global jobs.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Word-boundary India check as a safety net in case the WID facet ever
# breaks for this tenant (see Micron/Verizon/Lowe's/PayPal/FactSet bugs in
# the playbook) — never matches "Indianapolis"/"Indiana".
_INDIA_RE = re.compile(r"\bindia\b", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_TENANT_HOST}/External",
}


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

    # "posted 30+ days ago" → treat as 30 days
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


def _job_id_from_posting(posting: dict) -> str:
    """Extract the JR_ requisition ID from a search-result posting."""
    for field in posting.get("bulletFields", []) or []:
        m = re.match(r"^(JR_\d+)$", str(field).strip(), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    # Fallback: trailing _JR_NNNNN(-N)? segment of the externalPath
    m = re.search(r"_(JR_\d+(?:-\d+)?)$", posting.get("externalPath", ""), re.IGNORECASE)
    if m:
        return m.group(1).upper()
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
    # Workday rejects limit > 20 for this tenant with HTTP 400 (same cap as
    # Northern Trust/Nvidia/Walmart) — clamp defensively.
    limit = min(max(int(num), 1), _PAGE_SIZE)

    body = {
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
        "limit": limit,
        "offset": start,
        "searchText": keyword or "",
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
                raise RateLimitError("Guidewire Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Guidewire fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = _job_id_from_posting(p)
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()

        # Safety net: skip non-India results in case the WID facet ever
        # breaks for this tenant, same discipline as Micron/Verizon/Lowe's.
        if not _INDIA_RE.search(loc):
            continue

        app_url = f"{_PUBLIC_JOB_BASE}{external_path}" if external_path else ""

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

    application_url is the public wd5.myworkdaysite.com page; it's rewritten
    to the CXS JSON API host/path to get the full description without a
    browser. Returns (description_text, posting_date).
    """
    ext_path = ""
    if _PUBLIC_JOB_BASE in application_url:
        ext_path = application_url[len(_PUBLIC_JOB_BASE):]
    elif "/External/" in application_url:
        ext_path = "/" + application_url.split("/External/", 1)[-1]
    else:
        m = re.search(r"/job/.*$", application_url)
        if m:
            ext_path = m.group(0)

    if not ext_path:
        return "", ""

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
                raise RateLimitError("Guidewire description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Guidewire description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    # startDate is already YYYY-MM-DD from the API; fall back to the
    # relative postedOn string if it's ever missing.
    posting_date = info.get("startDate", "") or _parse_posted_on(info.get("postedOn", ""))

    return description, posting_date
