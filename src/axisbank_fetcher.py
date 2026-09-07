"""Fetches Axis Bank India job listings via RippleHire.

Axis Bank's marketing careers page (axisbank.com/careers) redirects to a
RippleHire-hosted candidate site (axisbank.ripplehire.com/candidate/?token=
WIXhCuz0XRZ7H0GZCwjJ&source=CAREERSITE#list) for the actual job board --
same underlying ATS/API shape as HDFC Bank/CitiusTech/LTIMindtree already in
this repo:

  - POST /candidate/candidatejobsearch with a JSON-encoded
    `careerSiteUrlParams` form field (page, search, token, source, pagesize)
    returns jobVoList + totalJobCount. The `search` keyword genuinely
    narrows server-side (confirmed live 2026-09-06: a nonsense token
    `zzznonsensequeryabc` returns 0; "c# developer" and "angular" both
    return 0 too -- a genuine current zero for those two specific terms, not
    a fetcher bug; "software engineer"=80, "senior software engineer"=240,
    ".net developer"/"dot net"=632, "ai engineer"="engineer"=13,
    "machine learning engineer"=2924 (RippleHire's OR-token matching --
    "machine" and "learning" and "engineer" separately hit a huge swath of
    this tenant's ~350+-strong branch/RM/teller pool), "python developer"=28,
    "generative ai engineer"=13).
  - Search results carry no posting date and a null jobDesc; a separate
    GET /candidate/candidatejobdetail?jobSeq={id} has the real
    jobPostingDate ("DD-Mon-YYYY") plus the full jobDesc HTML, same shape as
    HDFC Bank/CitiusTech.
  - Requires a Referer header matching the candidate site or the API
    returns an empty/non-JSON body instead of real JSON.

**Signal-to-noise (per the onboarding brief's "IT careers vs. generic branch
hiring" concern)**: axisbank.ripplehire.com is Axis's single, bank-wide
RippleHire tenant, and is considerably NOISIER than HDFC Bank's equivalent
tenant -- across a ~350-job sample the overwhelming majority are retail
branch/teller/relationship-officer and "RL - Wheels" auto-loan sales-manager
reqs scattered across hundreds of small Indian towns. A genuine, separately-
labelled technology signal does exist under two departments:
  - "IT:" department reqs (Mumbai-based): "Tech Engineer - Development"
    (x3), "Tech Manager - Development" (x2), "Principal - Technology
    Solution", "Tech Engineer - Technology Solution".
  - "BIU:" (Business Intelligence Unit, Mumbai/Bangalore-based): a real
    data-engineering/data-science bench -- "Data Engineer-Compliance"
    (x3), "Data Engineer-RD- Products & Portfolio" (x2), "Data Engineer-
    RL&P- SBB", "Data Science-Personalization COE", "Data Science Lead-
    Personalization COE", "Data Science-Alternate Data", "Data Science-
    Model Risk Mgmt and Financial Modelling", "Data Scientist-Model Risk
    Mgmt and Financial Modelling", "Business Analyst-*" (a dozen+ variants),
    "Engineering & Reporting Head-Central BIU".
No separate "Axis Bank technology careers" portal was found live -- this
fetcher relies on the shared title/skill matcher to separate the IT/BIU
signal from the much larger branch-hiring noise.

India detection (no location facet, no reliable country param -- Axis has a
small number of overseas/GIFT City presences not observed in the live
sample used to build this fetcher):
  - `locations` is a bare city/region string (e.g. "Mumbai", "BANGALORE"
    [note: inconsistent casing on this tenant, unlike HDFC's tenant], "New
    Delhi", "Bilaspur, Bilaspur") with no "india" substring anywhere --
    normalization is required or every job would be silently dropped by
    matcher.py's `is_india_job()`.
  - Recognized Indian city/state tokens get ", India" appended (case-
    insensitively); a small set of known overseas tokens (dubai, bahrain,
    hong kong, singapore, london, gift city, "new york", "usa") are
    explicitly left unmodified (fails `is_india_job()` safely) should Axis
    ever post an overseas req through this same tenant; anything else
    unrecognized is also left unmodified rather than guessed at.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests

_TOKEN = "WIXhCuz0XRZ7H0GZCwjJ"
_BASE = "https://axisbank.ripplehire.com/candidate"
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
    "Referer": f"{_CANDIDATE_PAGE}#list",
}

# Known overseas signals -- left unmodified (fails is_india_job() safely)
# rather than guessed at, same defensive spirit as CitiusTech/HDFC Bank.
_NON_INDIA_TOKENS = (
    "dubai", "bahrain", "hong kong", "singapore", "london", "gift city",
    "new york", "usa",
)


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


# Pagination-guard state: page-0 first job IDs, per this process run.
_FIRST_PAGE_IDS: set[str] | None = None


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Axis Bank jobs matching *keyword*.

    RippleHire paginates by 0-indexed `page` + `pagesize`, not start/num
    offsets -- page is derived assuming a constant page size across calls.
    India filtering happens in matcher.py via `is_india_job()`; this
    function only normalizes location text.
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
                raise RateLimitError("Axis Bank: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Axis Bank fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Axis Bank fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("Axis Bank: non-JSON response (missing Referer?)")

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
    """Return (description, posting_date) for a single Axis Bank job."""
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
                raise RateLimitError("Axis Bank description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Axis Bank description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Axis Bank description fetch: no response — {last_exc}")

    try:
        job_vo = r.json().get("jobVO", {})
    except ValueError:
        raise RateLimitError("Axis Bank description: non-JSON response")

    parts = [_strip_html(job_vo.get("jobSkills", "")), _strip_html(job_vo.get("jobDesc", ""))]
    description = " ".join(p for p in parts if p)
    posting_date = _parse_date(job_vo.get("jobPostingDate", "") or "")
    return description, posting_date
