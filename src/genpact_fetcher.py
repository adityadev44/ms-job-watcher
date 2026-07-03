"""Fetches Genpact job listings via the Workday public REST API.

Genpact's ATS is Workday, hosted at genpact.wd108.myworkdayjobs.com, site
External_Careers. Same CXS REST pattern as Wells Fargo/Citi/etc — no
Playwright needed.

Unlike most Workday tenants covered here, Genpact has no usable
locationCountry (or similarly-named) facet — the only facets exposed are
jobFamilyGroup/workerSubType/timeType/locationMainGroup, and
locationMainGroup returns no India sub-value. Jobs are fetched globally per
keyword (searchText server-side narrows the ~2000-capped total significantly)
and filtered for India client-side via locationsText.
"""
from __future__ import annotations

import re
import time
import warnings

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://genpact.wd108.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/genpact/External_Careers/jobs"
_JOB_BASE = f"{_BASE_URL}/External_Careers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/genpact/External_Careers"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/External_Careers",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    body = {
        "appliedFacets": {},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout, verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Genpact: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Genpact fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Genpact fetch: no response — {last_exc}")

    jobs: list[dict] = []
    for p in r.json().get("jobPostings", []):
        loc = (p.get("locationsText") or "").strip()
        if "india" not in loc.lower():
            continue

        external_path = p.get("externalPath", "")
        m = re.search(r"_([A-Za-z]+\d+)$", external_path)
        job_id = m.group(1).upper() if m else ""
        if not job_id:
            continue

        title = (p.get("title") or "").strip()
        if not title:
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": "",  # filled in on description fetch (startDate)
            "application_url": f"{_JOB_BASE}{external_path}" if external_path else "",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/External_Careers/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Genpact description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Genpact description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Genpact description fetch: no response — {last_exc}")

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""
    return description, posting_date
