"""Fetches Deutsche Bank job listings via the Beesite API (careers.db.com).

Deutsche Bank's careers site uses a Beesite JSON search API at
api-deutschebank.beesite.de/search/. The underlying ATS is Workday
(db.wd3.myworkdayjobs.com) — each job's ApplyURI points there and the
Workday CXS detail API provides full job descriptions.

Key quirks:
- Keywords and country filters are ignored server-side; all queries return
  the same global pool (~1808 jobs). Country filtering is done client-side
  by checking PositionLocation[0].CountryCode == "IN".
- The API is paginated with up to 500 items per request; ~4 pages cover all
  jobs. Results are cached in-module after the first call so repeated keyword
  calls (from find_matching_jobs) are free.
- Location strings from Beesite are city-only (e.g. "Bangalore"). ", India"
  is appended so matcher.py's is_india_job() check passes.
- Job descriptions come from the Workday CXS detail API (same pattern as
  wellsfargo_fetcher.py): strip /apply from ApplyURI, then replace the
  /DBWebsite/ prefix with /wday/cxs/db/DBWebsite/.
"""
from __future__ import annotations

import time
import warnings

import requests
from bs4 import BeautifulSoup

_BEESITE_URL = "https://api-deutschebank.beesite.de/search/"
_WD_JOB_BASE = "https://db.wd3.myworkdayjobs.com/DBWebsite"
_WD_CXS_BASE = "https://db.wd3.myworkdayjobs.com/wday/cxs/db/DBWebsite"
_BEESITE_PAGE_SIZE = 500   # max items per Beesite request
_BEESITE_MAX_ITEMS = 2500  # safety ceiling

_BEESITE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://careers.db.com",
    "Referer": "https://careers.db.com/",
}

_WD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": "https://db.wd3.myworkdayjobs.com/DBWebsite",
}

# Module-level cache: populated on first call, reused for all keyword passes.
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _fill_cache(timeout: int = 30) -> None:
    """Fetch all Deutsche Bank jobs from Beesite and cache India ones.

    Sets _cache_filled = True BEFORE the try block to prevent a retry storm
    if the first call fails (same pattern as honeywell_fetcher.py fix).
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return

    # Mark filled first — even on failure the cache stays empty rather than
    # triggering a refetch on every subsequent keyword call.
    _cache_filled = True

    collected: list[dict] = []
    first_item = 1

    try:
        while first_item <= _BEESITE_MAX_ITEMS:
            payload = {
                "LanguageCode": "EN",
                "SearchParameters": {
                    "FirstItem": first_item,
                    "CountItem": _BEESITE_PAGE_SIZE,
                    "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
                    "MatchedObjectDescriptor": [
                        "ID",
                        "PositionTitle",
                        "ApplyURI",
                        "PositionLocation.CountryCode",
                        "PositionLocation.CityName",
                        "PublicationStartDate",
                    ],
                },
                "SearchCriteria": [],
            }

            for attempt in range(3):
                try:
                    r = requests.post(
                        _BEESITE_URL,
                        json=payload,
                        headers=_BEESITE_HEADERS,
                        timeout=timeout,
                    )
                    if r.status_code == 429:
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        raise RateLimitError("Deutsche Bank Beesite: 429 rate-limited")
                    r.raise_for_status()
                    break
                except RateLimitError:
                    raise
                except requests.RequestException as exc:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError(
                        f"Deutsche Bank Beesite fetch failed: {exc}"
                    ) from exc

            data = r.json()
            items = data.get("SearchResult", {}).get("SearchResultItems", [])
            if not items:
                break

            for item in items:
                desc = item.get("MatchedObjectDescriptor", {})
                loc_list = desc.get("PositionLocation", [])
                if not loc_list:
                    continue

                country_code = loc_list[0].get("CountryCode", "")
                if country_code != "IN":
                    continue  # Skip non-India jobs

                job_id = item.get("MatchedObjectId", "")
                if not job_id:
                    continue

                title = desc.get("PositionTitle", "").strip()
                if not title:
                    continue

                city = loc_list[0].get("CityName", "").strip()
                location_str = f"{city}, India" if city else "India"

                apply_uris = desc.get("ApplyURI", [])
                apply_uri = apply_uris[0] if apply_uris else ""
                # Strip /apply to get the job page URL (Workday convention)
                app_url = apply_uri.replace("/apply", "") if apply_uri else ""

                posting_date = desc.get("PublicationStartDate", "") or ""

                collected.append({
                    "id": job_id,
                    "title": title,
                    "location": location_str,
                    "posting_date": posting_date,
                    "application_url": app_url,
                })

            total_claimed = data.get("SearchResult", {}).get("SearchResultCountAll", 0)
            first_item += _BEESITE_PAGE_SIZE
            if first_item > total_claimed:
                break

    except RateLimitError:
        raise
    except Exception as exc:
        raise RateLimitError(
            f"Deutsche Bank cache fill failed: {exc}"
        ) from exc

    _india_cache = collected
    print(
        f"[Deutsche Bank] Cache filled: {len(collected)} India jobs "
        f"from Beesite API"
    )


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Deutsche Bank India jobs.

    Keywords and location are ignored server-side; the cache holds all India
    jobs fetched in one pass. Pagination via start/num slices the cache so
    find_matching_jobs terminates naturally when the slice is empty.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    application_url format:
      https://db.wd3.myworkdayjobs.com/DBWebsite/job/{path}/{title}_{reqId}

    CXS detail URL pattern (same as wellsfargo_fetcher / fidelity_fetcher):
      https://db.wd3.myworkdayjobs.com/wday/cxs/db/DBWebsite/job/{path}/...

    Returns (description_text, posting_date_YYYY-MM-DD).
    """
    if _WD_JOB_BASE in application_url:
        ext_path = application_url[len(_WD_JOB_BASE):]
    else:
        # Fallback: try to extract path after /DBWebsite/
        ext_path = "/" + application_url.split("/DBWebsite/", 1)[-1]
    api_url = f"{_WD_CXS_BASE}{ext_path}"

    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    api_url,
                    headers=_WD_HEADERS,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Deutsche Bank Workday description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Deutsche Bank description fetch failed: {exc}"
            ) from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )

    # startDate is already YYYY-MM-DD from the Workday CXS API
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
