"""Fetches HDFC Bank India job listings via RippleHire.

HDFC Bank's marketing careers page (hdfc.bank.in/careers) links out to a
RippleHire-hosted candidate site (hdfcbank.ripplehire.com) for the actual
job board -- same underlying ATS/API shape as CitiusTech/LTIMindtree/
Mphasis already in this repo:

  - POST /candidate/candidatejobsearch with a JSON-encoded
    `careerSiteUrlParams` form field (page, search, token, source, pagesize)
    returns jobVoList + totalJobCount. The `search` keyword genuinely
    narrows server-side (confirmed live 2026-09-06: an empty search returns
    0 jobs -- keyword is REQUIRED on this tenant, same as CitiusTech; a
    nonsense token `zzznonsensequeryabc123` also returns 0; real keywords
    return real, differing counts -- "software engineer"=27,
    "senior software engineer"=52, ".net developer"=29, "c# developer"=6,
    "angular"=3, "ai engineer"=21, "machine learning engineer"=23,
    "python developer"=11, "generative ai engineer"=21, "dot net"=23).
  - RippleHire's own keyword matching is loose/OR-based across tokens (e.g.
    "ai engineer" and "machine learning engineer" both surface the same
    ~20 "Tech & Digital-*Engineer*" reqs as plain "engineer" would) --
    over-inclusive, not under-inclusive, which is the safe direction since
    matcher.py's own title/skill filters do the real precision work
    downstream.
  - Search results carry no posting date and a null jobDesc; a separate
    GET /candidate/candidatejobdetail?jobSeq={id} has the real
    jobPostingDate ("DD-Mon-YYYY", e.g. "03-Sep-2026") plus the full jobDesc
    HTML (this tenant's JDs are genuinely rich -- observed skill sections
    literally list "C# / VB.NET / JAVA / Xamarin form / ASP.NET / .Net
    Core / Python / C++ / Ruby / Angular / React").
  - Requires a Referer header matching the candidate site or the API
    returns an empty/non-JSON body instead of real JSON.

**Signal-to-noise (per the onboarding brief's "IT careers vs. generic branch
hiring" concern)**: hdfcbank.ripplehire.com is HDFC's single, bank-wide
RippleHire tenant -- it is NOT a separate IT-only portal, and does mix a
large volume of retail branch/relationship-manager/sales reqs (BBWC-RM, RM-
Wealth, Branch Transformation Mgr, Collections Mgr, etc., scattered across
dozens of small Indian towns) with a genuine, clearly-labelled "Tech &
Digital-*" title family (e.g. "Tech & Digital-Software Engineer-Full Stack",
"Tech & Digital-Sr Software Engineer-Backend/Frontend/Full Stack", "Tech &
Digital-Lead Software Engineer-Backend/Frontend", "Tech & Digital-Engineering
Manager", "Tech & Digital-Lead Technology Architect", "Tech & Digital-Sr
QA/Database/Data Engineer", "Tech & Digital-Lead- Cloud Network & Security
Engineer") concentrated in Bangalore/Navi Mumbai/Gurgaon. No separate
"HDFC Bank IT careers" portal was found live (the generic careers page has
no such link) -- this fetcher relies on the shared title/skill matcher to
separate the "Tech & Digital-" signal from the branch-hiring noise, same as
CitiusTech's tenant.

India detection (no location facet, no reliable country param -- HDFC has
a small number of overseas branches, e.g. Bahrain/Hong Kong/Dubai/London,
though none were observed in the live India-city sample used to build this
fetcher):
  - `locations` is a bare city/region string (e.g. "Mumbai", "Bangalore",
    "Gurgaon", "Raigarh, Raipur, Bilaspur", "DHANBAD - JHARKHAND") with no
    "india" substring anywhere -- normalization is required or every job
    would be silently dropped by matcher.py's `is_india_job()`.
  - Recognized Indian city/state tokens get ", India" appended; a small set
    of known overseas tokens (dubai, bahrain, hong kong, singapore, london,
    "new york", "usa") are explicitly left unmodified (fails
    `is_india_job()` safely) should HDFC ever post an overseas req through
    this same tenant; anything else unrecognized is also left unmodified
    rather than guessed at.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests

_TOKEN = "pvB5iAMcmu4ydUh2IW2O"
_BASE = "https://hdfcbank.ripplehire.com/candidate"
_SEARCH_URL = f"{_BASE}/candidatejobsearch"
_DETAIL_URL = f"{_BASE}/candidatejobdetail"
_CANDIDATE_PAGE = f"{_BASE}/?token={_TOKEN}&lang=en&source=CAREERSITE"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": _CANDIDATE_PAGE,
}

# Known overseas signals -- HDFC Bank has a handful of international
# branches; left unmodified (fails is_india_job() safely) rather than
# guessed at, same defensive spirit as CitiusTech.
_NON_INDIA_TOKENS = ("dubai", "bahrain", "hong kong", "singapore", "london", "new york", "usa")

# Pagination-guard state: page-0 first job IDs, per this process run.
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-Mon-YYYY' (e.g. '03-Sep-2026') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_location(raw: str) -> str:
    loc = (raw or "").strip()
    if not loc:
        return loc
    low = loc.lower()
    if "india" in low:
        return loc
    if any(tok in low for tok in _NON_INDIA_TOKENS):
        return loc
    return f"{loc}, India"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of HDFC Bank jobs matching *keyword*.

    RippleHire requires a non-empty `search` term (an empty search returns
    0 jobs on this tenant) and paginates by 0-indexed `page` + `pagesize`,
    not start/num offsets -- page is derived assuming a constant page size
    across calls. India filtering happens in matcher.py via
    `is_india_job()`; this function only normalizes location text.
    """
    global _FIRST_PAGE_IDS

    if not keyword:
        return []

    page_num = start // num if num else 0
    params = {
        "page": page_num,
        "search": keyword,
        "token": _TOKEN,
        "source": "CAREERSITE",
        "pagesize": num,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(
                _SEARCH_URL,
                data={"careerSiteUrlParams": json.dumps(params), "lang": "en"},
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("HDFC Bank: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"HDFC Bank fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"HDFC Bank fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("HDFC Bank: non-JSON response (missing Referer?)")

    jobs: list[dict] = []
    for job in data.get("jobVoList", []) or []:
        job_id = job.get("jobSeq") or job.get("jobId")
        title = (job.get("jobTitle") or "").strip()
        if not (job_id and title):
            continue
        jobs.append({
            "id": str(job_id),
            "title": title,
            "location": _normalize_location(job.get("locations") or ""),
            "posting_date": "",  # not present in search results; filled on detail fetch
            "application_url": f"{_CANDIDATE_PAGE}#detail/job/{job_id}",
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
    """Return (description, posting_date) for a single HDFC Bank job."""
    m = re.search(r"#detail/job/(\d+)", application_url)
    job_seq = m.group(1) if m else ""

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _DETAIL_URL,
                params={
                    "token": _TOKEN,
                    "jobSeq": job_seq,
                    "source": "CAREERSITE",
                    "lang": "en",
                },
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("HDFC Bank description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"HDFC Bank description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"HDFC Bank description fetch: no response — {last_exc}")

    try:
        job_vo = r.json().get("jobVO", {})
    except ValueError:
        raise RateLimitError("HDFC Bank description: non-JSON response")

    parts = [_strip_html(job_vo.get("jobSkills", "")), _strip_html(job_vo.get("jobDesc", ""))]
    description = " ".join(p for p in parts if p)
    posting_date = _parse_date(job_vo.get("jobPostingDate", "") or "")
    return description, posting_date
