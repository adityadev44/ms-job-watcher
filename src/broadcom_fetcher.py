"""Fetches Broadcom Inc. job listings via the Workday public REST API.

ATS identification (Step 1, verified live 2026-09-13, not guessed):
broadcom.wd1.myworkdayjobs.com/External_Career returns HTTP 200 and
POST /wday/cxs/broadcom/External_Career/jobs returns real jobPostings
(369 global jobs at verification time) — standard Workday CXS tenant.

Same "no flat country facet" shape as Intel/Genpact/Nvidia — only
``locationMainGroup`` exists, nesting an inner ``locations`` facet of
individual site entries. Unlike Intel (one India site) or Nvidia (one India
WID), Broadcom's India presence is split across FOUR separate site entries
(reflecting both the legacy Broadcom semiconductor business and the VMware
by Broadcom software business, acquired 2023, which shares this same
tenant): "IND-Bangalore Electronic City - S1" (36), "IND-Bangalore-Kalyani
Vista II" (7), "IND-Hyderabad 115 IT Park Area" (16), and
"India-Bangalore-Remote Location" (1) — 60 jobs total, confirmed by
requesting all four WIDs together in one ``appliedFacets.locations`` list
(Workday facets support multi-select this way) and checking every
``locationsText`` genuinely names one of these four sites.

Same "HTTP 400 above limit 20" quirk as Intel/Nvidia — clamped to 20 here;
plain offset pagination (0/20/40) runs out cleanly at this pool size (no
wraparound observed, same as Intel). Keyword search (``searchText``) is
genuinely server-side — not added to ``_IGNORES_KEYWORDS``.

Because this tenant now spans both a chip-design (RTL/ASIC/verification)
GCC and VMware's cloud/enterprise-software India GCC, the India pool is
richer in genuine software roles than a pure-semiconductor peer (Intel/TI):
confirmed live matches include "Principal Member of Technical Staff – AI
Platform & ValueOps Financial Intelligence Solution" (Bangalore — explicitly
names "generative ai" in its JD body, a real ``AI / ML / Python`` primary-
skill match; "member of technical staff" is already in the shared
``title_family`` list) and a "Senior Software Architect" (Hyderabad) whose
JD explicitly names LangChain, LangGraph, and vector databases — a genuinely
strong AI/ML JD that currently fails only `title_family` ("architect" isn't
a covered phrase), the same "flag, don't silently patch" precision gap
already documented for other companies in PLAYBOOK.md, not a fetcher issue.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://broadcom.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/broadcom/External_Career/jobs"
_JOB_BASE = f"{_BASE_URL}/External_Career"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/broadcom/External_Career"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# The four India site WIDs under the nested "locations" facet (see
# docstring) -- tenant-specific, not the shared cross-tenant India GUID.
_INDIA_WIDS = [
    "2a204116f85f01189cd964af936b70fb",  # IND-Bangalore Electronic City - S1
    "877d747df71910021363ea290d900000",  # IND-Bangalore-Kalyani Vista II
    "0dd627624e2e0176f301cad8dcd9ab0b",  # IND-Hyderabad 115 IT Park Area
    "752a3c9efe39105be2259b2c18d3b6e4",  # India-Bangalore-Remote Location
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
    "Referer": f"{_BASE_URL}/External_Career",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


def _parse_posted_on(posted_on: str) -> str:
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
    body = {
        "appliedFacets": {"locations": _INDIA_WIDS},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(_SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Broadcom Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Broadcom fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    jobs: list[dict] = []
    for p in postings:
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()
        if "india" not in loc.lower():
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


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date). startDate is already ISO
    (YYYY-MM-DD), no relative-date conversion needed.
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/External_Career/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                raise RateLimitError("Broadcom description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Broadcom description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
