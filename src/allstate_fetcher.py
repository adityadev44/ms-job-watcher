"""Fetches Allstate job listings via the Workday public REST API.

Allstate's ATS is Workday, hosted at allstate.wd5.myworkdayjobs.com (site
code allstate_careers). The Workday CXS endpoint accepts plain POST requests
— no browser needed, same pattern as Wells Fargo/Marsh McLennan/Mastercard.

India is filtered server-side using the Country facet WID (confirmed the
same cross-tenant GUID as Wells Fargo/Citi's India facet, though Allstate
exposes it under the plain "Country" facet key rather than
"locationCountry"). Allstate India (set up 2012) is a genuine two-city GCC —
verified live: Bengaluru ("Ind – Blr Sez 1/2 ...") and Pune ("Ind – Pune
Sez 1 ..." / bare "Pune") postings both present, so this is NOT a
Pune-only structural zero. Total India pool is small (~7 jobs as of
2026-09-13) — mostly non-tech (Customer Service, Reconciliations, SAP
security); current .NET/C#/AI-ML matches may be zero, same class as
AIG/Swiss Re/ANZ (see PLAYBOOK.md) rather than a fetcher bug.

Same "offset ignored, always returns page 1" wraparound bug as UBS/
MUFG/Nvidia/PepsiCo: passing a non-zero ``offset`` still returns the
identical first page instead of an empty tail or the next slice, so the
generic matcher.py pagination loop (which advances ``start`` by the
returned page length each iteration) never naturally terminates within a
single keyword pass until it hits ``max_listings`` — verified live this
made one full run_company pass take minutes for a 7-job pool. Fixed the
same way as Deutsche Bank/Persistent/PepsiCo: cache the entire India pool
once per process (single unfiltered request) and slice it locally by
offset — this also sidesteps ``searchText`` entirely, which is fine since
the whole pool is tiny enough that keyword narrowing isn't needed for
efficiency (registered in ``_IGNORES_KEYWORDS``).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://allstate.wd5.myworkdayjobs.com"
_SITE = "allstate_careers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/allstate/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_PAGE_SIZE = 20

# India Country facet WID for Allstate's Workday tenant (stable GUID; same
# value observed at Wells Fargo/Citi's tenants under different facet key
# names). Verified 2026-09-13: returns 7 India results with no keyword.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/{_SITE}",
}

# Detail API base — same CXS prefix, job-specific path appended
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/allstate/{_SITE}"


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


# ---------------------------------------------------------------------------
# Cache-once fetch — see module docstring for why (offset is ignored by
# this Workday tenant; pagination must be done locally after one full pull).
# ---------------------------------------------------------------------------

_cache: list[dict] = []
_cache_filled = False


def _fill_cache(timeout: int) -> None:
    global _cache_filled
    _cache_filled = True  # set before the try — a failed fill must not retry forever
    body = {
        # Workday rejects limit > 20 with a 400 on this tenant (confirmed
        # live: limit=50 -> 400 Bad Request). The full India pool (7 jobs
        # as of 2026-09-13) fits in one page at the max allowed size.
        "appliedFacets": {"Country": [_INDIA_WID]},
        "limit": 20,
        "offset": 0,
        "searchText": "",
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
                raise RateLimitError("Allstate Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Allstate fetch failed: {exc}") from exc

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        # Job ID from bulletFields (e.g. ["R34543"]) — Allstate's requisition
        # IDs have no hyphen, unlike Wells Fargo's "R-542087".
        job_id = ""
        for field in p.get("bulletFields", []):
            m = re.match(r"^(R\d+)$", field.strip(), re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
                break
        if not job_id:
            # Fallback: extract from externalPath end segment
            m = re.search(r"_(R\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
            if m:
                job_id = m.group(1).upper()
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()

        # Allstate's own India location strings say "Ind – Blr/Pune Sez
        # ..." or even bare "Pune" rather than "India" — a plain "india"
        # substring check (or requiring an "ind" prefix) would silently
        # drop genuine results (verified: bare "Pune" fails both). The
        # Country facet WID above is confirmed reliable here (its result
        # count exactly matches the known Bengaluru/Pune breakdown), so
        # trust it as the sole India gate and just normalize the display
        # string for matcher.py's is_india_job() check.
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc or "India",
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    # offset is confirmed non-functional on this tenant (see module
    # docstring), so there is no way to page past the first 20 results if
    # the India pool ever grows beyond that. Flag it loudly rather than
    # silently truncating if it ever happens.
    total = r.json().get("total", 0)
    if total > len(jobs):
        print(
            f"  [warn] Allstate India pool reports total={total} but only "
            f"{len(jobs)} fit in one page and offset pagination does not "
            "work on this tenant — some jobs are not visible to this fetcher"
        )

    _cache.clear()
    _cache.extend(jobs)


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
    if not _cache_filled:
        _fill_cache(timeout)
    return _cache[start:start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    The application_url (HTML page) is transformed to the JSON API path.
    Returns (description_text, posting_date).
    """
    # Transform application URL to JSON API URL:
    # https://allstate.wd5.myworkdayjobs.com/allstate_careers/job/...
    # → https://allstate.wd5.myworkdayjobs.com/wday/cxs/allstate/allstate_careers/job/...
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(2):
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
                raise RateLimitError("Allstate description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Allstate description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    # startDate is already YYYY-MM-DD from the API
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
