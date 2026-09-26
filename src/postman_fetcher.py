"""
Postman job fetcher — Workday CXS REST API.

Careers site: https://postman.wd108.myworkdayjobs.com/careers
ATS confirmed live (2026-09-26): Workday — Postman migrated away from
Greenhouse (board token "postman" now returns 404) to their own Workday
tenant at postman.wd108.myworkdayjobs.com.

**Previous ATS (now dead)**: Greenhouse board token "postman" returned 404
as of 2026-09-26. The board page (job-boards.greenhouse.io/postman) and the
jobs listing API (boards-api.greenhouse.io/v1/boards/postman/jobs) are gone.
Individual job URLs (job-boards.greenhouse.io/postman/jobs/<id>) redirect to
/postman?error=true. Postman job listings on third-party aggregators (Jobfound,
etc.) now link to https://postman.wd108.myworkdayjobs.com/careers/job/...

Search endpoint: POST https://postman.wd108.myworkdayjobs.com/wday/cxs/postman/careers/jobs
  Body: {"limit": <n>, "offset": <start>, "searchText": ""}
  - The server returns all Postman jobs (38 as of 2026-09-26). This tenant
    appears to be Postman's India talent acquisition portal — all observed
    postings are in Bangalore, India, with "india" already present in the
    locationsText field, making filtering straightforward.
  - `searchText` is accepted but this fetcher does not use it server-side
    (matcher.py handles keyword matching on titles client-side, same pattern
    as other Workday fetchers in this repo that don't use country facets).
  - Server hard-caps `limit` at 20 (requesting 50 returns empty results).
    Pagination walks `offset` in steps of 20.
  - `bulletFields[0]` carries the requisition ID (e.g., "JR1000054").

Job detail: GET https://postman.wd108.myworkdayjobs.com/wday/cxs/postman/careers/job/<external_path>
  Returns `jobPostingInfo.jobDescription` (HTML, stripped inline) and
  `jobPostingInfo.startDate` (ISO-8601, e.g., "2026-09-25").

Live-verified 2026-09-26:
- 38 total postings; all observed locations are "Bangalore, India".
- Example roles: "Senior Engineer - AI", "Software Engineer (Frontend UI),
  Git Native", "Software Engineer (Frontend Platform), Git Native".

India detection: locationsText already contains "india" (e.g., "Bangalore,
India"), so a simple case-insensitive substring check is sufficient.

Descriptions: fetched per-job from the Workday CXS detail endpoint on demand.
Results are cached in `_desc_cache` to avoid duplicate HTTP calls.
"""
from __future__ import annotations

import html as _html_mod
import re
import time
import warnings

import requests
from bs4 import BeautifulSoup


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""


_BASE_URL = "https://postman.wd108.myworkdayjobs.com"
_TENANT = "postman"
_SITE = "careers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_SITE}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/{_TENANT}/{_SITE}"

_MAX_LIMIT = 20  # Workday hard-caps at 20 for this tenant
_MAX_PAGES = 20  # defensive cap against runaway pagination (20 * 20 = 400 jobs)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": _JOB_BASE,
}

# Module-level cache: the Workday board is fetched once per process.
_india_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled: bool = False

# Pagination-wraparound guard
_FIRST_PAGE_IDS: set[str] | None = None


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_posted_on(posted_on: str) -> str:
    """Convert Workday relative date string to YYYY-MM-DD."""
    from datetime import date, timedelta

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


def _post_with_retries(body: dict, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL, headers=_HEADERS, json=body,
                    timeout=timeout, verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Postman: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Postman request failed: {exc}") from exc
    raise RateLimitError(f"Postman request failed: {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch all Postman Workday jobs once and cache India postings.

    `_cache_filled` is set True before the network call so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()
    call within the same process (Honeywell/Persistent lesson).
    """
    global _cache_filled, _india_cache
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None
    total_fetched = 0

    for page_num in range(_MAX_PAGES):
        offset = page_num * _MAX_LIMIT
        body = {"limit": _MAX_LIMIT, "offset": offset, "searchText": ""}
        try:
            r = _post_with_retries(body, timeout)
        except RateLimitError:
            raise

        postings = r.json().get("jobPostings", [])
        if not postings:
            break

        page_ids = set()
        for p in postings:
            bullet_fields = p.get("bulletFields") or []
            external_path = p.get("externalPath", "")
            job_id = bullet_fields[0] if bullet_fields else ""
            if not job_id:
                # Fallback: extract requisition ID from external path
                m = re.search(r"_([A-Za-z]+-?\d+)$", external_path)
                job_id = m.group(1).upper() if m else ""
            if not job_id:
                continue

            page_ids.add(job_id)

            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            total_fetched += 1

            loc_name = (p.get("locationsText") or "").strip()
            if "india" not in loc_name.lower():
                continue  # skip non-India postings

            title = (p.get("title") or "").strip()
            if not title:
                continue

            posting_date = _parse_posted_on(p.get("postedOn") or "")
            apply_url = (
                f"{_JOB_BASE}{external_path}" if external_path else ""
            )
            collected.append({
                "id": job_id,
                "title": title,
                "location": loc_name,
                "posting_date": posting_date,
                "application_url": apply_url,
            })

        # Wraparound guard: stop if this page is identical to page 0
        if page_num == 0:
            first_page_ids = page_ids
        elif first_page_ids and page_ids == first_page_ids:
            break

        if len(postings) < _MAX_LIMIT:
            break  # last page

        time.sleep(0.2)

    _india_cache = collected
    print(f"[Postman] Cache filled: {len(collected)} India jobs (of {total_fetched} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Postman India jobs from the cached Workday board.

    keyword/location are accepted for interface compatibility but are not
    sent to the Workday API (this tenant's board returns all India postings
    regardless of query parameters). All keyword/title matching is handled
    by matcher.py after this call returns.
    """
    global _FIRST_PAGE_IDS

    _fill_cache(timeout=timeout)
    page = _india_cache[start: start + num]

    # Pagination-wraparound guard at the fetch_jobs level
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in page}
    elif _FIRST_PAGE_IDS and {j["id"] for j in page} == _FIRST_PAGE_IDS:
        return []

    return page


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) from the Workday CXS detail endpoint.

    description is the job description stripped of HTML.
    posting_date is in YYYY-MM-DD format (from jobPostingInfo.startDate).
    Results are cached per application_url.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Derive the CXS detail URL from the application URL.
    # application_url: https://postman.wd108.myworkdayjobs.com/careers/job/...
    # detail URL:      https://postman.wd108.myworkdayjobs.com/wday/cxs/postman/careers/job/...
    if f"{_BASE_URL}/{_SITE}/" in application_url:
        ext_path = application_url[len(f"{_BASE_URL}/{_SITE}"):]
    else:
        # Fallback: extract path after the site name
        ext_path = "/" + application_url.split(f"/{_SITE}/", 1)[-1]
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
                raise RateLimitError("Postman description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Postman description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Postman description: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Postman description: invalid JSON — {exc}") from exc

    info = data.get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "")
    description = _strip_html(raw_html)
    posting_date = (info.get("startDate") or "")[:10]  # ISO-8601 date, e.g. "2026-09-25"

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
