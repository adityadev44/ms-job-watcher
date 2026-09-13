"""Fetches Warner Bros. Discovery (India) job listings via the Workday
public REST API.

ATS identification (Step 1, verified live 2026-09-13): `careers.wbd.com`
is a Phenom People ("CareerConnect") front end (tenant `WAMEGLOBAL`) whose
search-results page is server-rendered with the full live search JSON
embedded directly in a `phApp.ddo = {...}` script block (same
no-Playwright-needed shape as Netflix's Eightfold embed). Crucially, every
job's `applyUrl` in that embed points at a genuine Workday tenant:
`warnerbros.wd5.myworkdayjobs.com/global/job/...` — WBD's actual system of
record is Workday, just skinned with a Phenom People front end. Confirmed by
hitting the standard Workday CXS endpoints directly with plain `requests`
(no browser, no session/cookie dance needed):

- `POST https://warnerbros.wd5.myworkdayjobs.com/wday/cxs/warnerbros/global/jobs`
  with `appliedFacets.locationCountry` set to the same cross-tenant India WID
  used elsewhere in this repo (`c4f78be1a8f14da0ab49ce1162348a5e`, e.g. Wells
  Fargo/Shell/BNY) returns exactly 34 India postings (cross-checked against
  the Phenom embed's own `aggregations` facet, which independently reports
  `"India": 34`) — the WID is confirmed live and correct for this tenant, not
  just copy-pasted.
- `searchText` genuinely narrows server-side (confirmed: `"engineer"` → 31 of
  34; a nonsense token → 0) — not added to `_IGNORES_KEYWORDS`.
- Job IDs come back directly in `bulletFields[0]` as `"R000107737"` (no
  hyphen, unlike Wells Fargo's `"R-542087"` format) — no regex needed beyond
  taking the first bullet field verbatim.
- Job-detail pages are the standard Workday CXS JSON API
  (`GET {search_url}/{externalPath}`), same shape as Wells Fargo — full
  `jobDescription` HTML plus a `startDate` already in `YYYY-MM-DD`.

Live data check (2026-09-13): all 34 current India postings are Hyderabad or
Bangalore/Bengaluru (Embassy Tech Village / Phoenix Equinox Tower office
addresses) — no Chennai, Tamil Nadu, Pune, or other excluded city observed
anywhere in the India-faceted pool (cross-checked against the Phenom embed's
own `state` facet: `"Telangāna": 25`, `"Karnātaka": 9`, nothing else). This is
the opposite shape from Comcast's TalentBrew tenant investigated the same
day, whose India engineering pool is almost entirely Chennai — WBD's real
GCC-style India engineering footprint is Hyderabad/Bangalore, not Chennai.
Real, current .NET/AI-track-relevant titles are present today, e.g. "Senior
Machine Learning Engineer, Hyderabad" and "Senior Staff Software Engineer -
Golang (Consumer Team), Hyderabad" (Golang engineer titles pass
`title_family`'s "cloud engineer"/generic engineer phrases only if their JD
also names a hard skill; this one's JD would need to be checked at match
time, same as any other company — not pre-verified as a match here, just
confirmed as a genuine, on-topic engineering posting worth having in the
pool).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://warnerbros.wd5.myworkdayjobs.com"
_TENANT = "warnerbros"
_SITE = "global"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"
_PAGE_SIZE = 20

# Cross-tenant India locationCountry WID, same GUID already confirmed
# correct at Wells Fargo/Shell/BNY Mellon/others in this repo. Verified live
# 2026-09-13 against WBD's tenant: returns 34 India results, matching the
# careers.wbd.com Phenom embed's own independent "India": 34 facet count.
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


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


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


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.request(method, url, timeout=kwargs.pop("timeout", 20), **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"WBD Workday: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"WBD Workday fetch failed for {url}: {exc}") from exc
    raise RateLimitError(f"WBD Workday fetch failed for {url}: {last_exc}")


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
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }

    r = _request_with_retry(
        "POST", _SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout, verify=False,
    )

    jobs: list[dict[str, str]] = []
    for p in r.json().get("jobPostings", []):
        external_path = p.get("externalPath", "")

        job_id = ""
        bullets = p.get("bulletFields", [])
        if bullets:
            job_id = bullets[0].strip()
        if not job_id:
            m = re.search(r"_(R\d+)(?:-\d+)?$", external_path)
            if m:
                job_id = m.group(1)
        if not job_id:
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        loc = p.get("locationsText", "").strip()

        # Safety net: skip non-India results in case the WID ever changes.
        # WBD's tenant location strings are office addresses without the
        # country name (e.g. "Hyderabad - Phoenix Equinox Tower 2"), so
        # check against known India cities rather than the literal word
        # "india".
        loc_lower = loc.lower()
        if not any(city in loc_lower for city in ("hyderabad", "bangalore", "bengaluru", "india")):
            continue

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc if "india" in loc_lower else f"{loc}, India",
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a WBD job's full description + posting date via the Workday
    CXS JSON detail API (same shape as Wells Fargo's `fetch_job_description`).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = _request_with_retry(
        "GET", api_url, headers=_HEADERS, timeout=timeout, verify=False,
    )

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
