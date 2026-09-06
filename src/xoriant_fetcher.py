"""
Xoriant job fetcher — Oracle Taleo Recruiting CE public REST API.

Careers site: https://www.xoriant.com/about-xoriant/careers
Taleo tenant: xoriant.taleo.net (Oracle Taleo Recruiting CE, legacy FTL-based
version "2026PRD.1.3.11.3.0"). External careers section: "ex", portal 101430233.

ATS discovery (2026-09-05): The Xoriant careers page links to:
  https://xoriant.taleo.net/careersection/ex/jobsearch.ftl?lang=en&portal=101430233

The FTL page is a JavaScript SPA shell — all job data is loaded client-side
via XHR. The underlying REST endpoint (extracted from SearchHandler.js) is:

  POST https://xoriant.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal=101430233
  Body: {"pageNo": <n>}
  Headers: tz, tzname (timezone hints, required by the Taleo backend)

Verified live (2026-09-05):
- pagingData.totalCount = 15 requisitions in the database.
- Only 1 requisition is publicly accessible via the REST API at this time
  (L2/L3 Protocols Testing, a networking QA role); the remaining 14 appear
  in the LOCATION facet counts but are not returned in requisitionList.
  This is a Taleo portal configuration choice — requisitions must be
  explicitly set to "posted externally" to appear in the public portal.
- No .NET/C#/AI/Python roles visible in the current public pool.
- Xoriant operates exclusively in India (Mumbai, Pune, Bangalore, Chennai,
  Hyderabad, Jaipur per corporate website), so all public postings are
  treated as India-based.

Pagination: Taleo uses 1-based page numbers with a fixed server page size
of 25. The `start` offset parameter is converted to a page number
(page = start // 25 + 1). Since the board is very small, all visible jobs
fit on page 1.

Field mapping from requisitionList entries:
  column[0] = job title
  column[1] = posting date (e.g. "Jun 18, 2026")
  column[2] = skill tags / keywords (e.g. "Networking Concepts^Network Security")

  `locationsColumns` = [] (empty in all observed responses — location is
  embedded in the description HTML which requires JavaScript to render).
  All jobs are tagged as "India" since Xoriant has no non-India offices.

Job detail URL: https://xoriant.taleo.net/careersection/ex/jobdetail.ftl?job=<contestNo>&lang=en&portal=101430233
  The FTL detail page is also JavaScript-rendered — its raw HTML has no
  job-specific data. `fetch_job_description` returns the skills/tags from
  column[2] as a description proxy (sufficient for keyword matching on
  technology terms embedded in those tags).

Keywords: NOT filtered server-side (the Taleo portal returns all visible
  requisitions regardless of keyword query). This is reflected in
  _IGNORES_KEYWORDS = True for the company registry.
Location: NOT filtered server-side (all returned jobs are India-based;
  location filter is irrelevant but applied client-side via the hardcoded
  "India" location string).
"""
from __future__ import annotations

import re
import time
import warnings

import requests


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""

_TENANT = "xoriant.taleo.net"
_PORTAL = "101430233"
_SECTION = "ex"
_SEARCH_URL = f"https://{_TENANT}/careersection/rest/jobboard/searchjobs?lang=en&portal={_PORTAL}"
_JOB_BASE = f"https://{_TENANT}/careersection/{_SECTION}/jobdetail.ftl"
_PAGE_SIZE = 25  # Taleo server-fixed page size

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": (
        f"https://{_TENANT}/careersection/{_SECTION}/jobsearch.ftl"
        f"?lang=en&portal={_PORTAL}"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "tz": "Asia/Kolkata",
    "tzname": "Asia/Kolkata",
}

# Module-level cache: the board is tiny (1 job currently), fetched once
# per process (same "cache-once + _cache_filled" pattern as ShareChat/Swiggy).
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}  # contestNo -> skills string
_cache_filled: bool = False

# Pagination-wraparound guard
_FIRST_PAGE_IDS: set[str] | None = None


def _parse_taleo_date(raw: str) -> str:
    """'Jun 18, 2026' -> '' (kept as-is; no standard ISO conversion here).

    Taleo dates are human-formatted strings. Return as-is; the matcher
    treats empty-string dates as acceptable.
    """
    # Leave as-is or attempt lightweight conversion
    if not raw:
        return ""
    # Try to parse "Jun 18, 2026" -> "2026-06-18"
    import datetime
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw.strip()[:10]


def _fill_cache(timeout: int = 20) -> None:
    """Fetch all Taleo pages and cache the job list.

    `_cache_filled` is set True before the first request to prevent retry
    storms on transient failures (Honeywell/Persistent lesson).
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    page = 1
    first_page_ids: set[str] = set()

    while True:
        last_exc: Exception | None = None
        r = None
        for attempt in range(3):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = requests.post(
                        _SEARCH_URL,
                        json={"pageNo": page},
                        headers=_HEADERS,
                        timeout=timeout,
                        verify=False,
                    )
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Xoriant: 429 rate-limited during cache fill")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    f"Xoriant cache fill page {page} failed: {exc}"
                ) from exc

        if r is None:
            raise RateLimitError(f"Xoriant cache fill page {page}: no response — {last_exc}")

        try:
            data = r.json()
        except ValueError as exc:
            raise RateLimitError(
                f"Xoriant cache fill page {page}: non-JSON — {exc}"
            ) from exc

        req_list = data.get("requisitionList", [])
        if not req_list:
            break

        page_ids: set[str] = set()
        for j in req_list:
            job_id = str(j.get("jobId") or "")
            contest_no = str(j.get("contestNo") or "")
            if not job_id:
                continue

            cols = j.get("column") or []
            title = (cols[0] if cols else "").strip()
            if not title:
                continue

            raw_date = cols[1].strip() if len(cols) > 1 else ""
            posting_date = _parse_taleo_date(raw_date)

            skills = cols[2].strip() if len(cols) > 2 else ""
            # Location: Xoriant is India-only (Mumbai, Pune, Bangalore,
            # Chennai, Hyderabad, Jaipur). All public postings are India.
            location_str = "India"

            app_url = (
                f"{_JOB_BASE}?job={contest_no}&lang=en&portal={_PORTAL}"
                if contest_no
                else f"{_JOB_BASE}?job={job_id}&lang=en&portal={_PORTAL}"
            )

            if skills:
                _desc_cache[app_url] = skills.replace("^", " ")

            collected.append({
                "id": job_id,
                "title": title,
                "location": location_str,
                "posting_date": posting_date,
                "application_url": app_url,
            })
            page_ids.add(job_id)

        # Wraparound guard: stop if page > 1 and same IDs as page 1
        if page == 1:
            first_page_ids = page_ids
        elif page_ids and page_ids == first_page_ids:
            break  # wraparound detected

        paging = data.get("pagingData", {})
        total_count = paging.get("totalCount", 0)
        page_size = paging.get("pageSize", _PAGE_SIZE) or _PAGE_SIZE
        if total_count and page * page_size >= total_count:
            break

        page += 1

    _job_cache[:] = collected
    print(f"[Xoriant] Cache filled: {len(collected)} public jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Xoriant jobs from the cached Taleo board.

    keyword/location are accepted for interface compatibility but ignored
    server-side — the Taleo portal returns all visible requisitions
    regardless of keyword query. All title/skill matching is done by
    matcher.py after this call returns.
    """
    global _FIRST_PAGE_IDS

    _fill_cache(timeout=timeout)
    page = _job_cache[start: start + num]

    # Pagination-wraparound guard
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in page}
    elif _FIRST_PAGE_IDS and {j["id"] for j in page} == _FIRST_PAGE_IDS:
        return []

    return page


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a Xoriant job.

    The Taleo FTL job-detail page is JavaScript-rendered — its raw HTML
    contains no job-specific data. The skills/tags string from the search
    response (column[2]) is returned as a description proxy; it is sufficient
    for keyword matching on technology skill terms.
    """
    _fill_cache(timeout=timeout)

    description = _desc_cache.get(application_url, "")

    posting_date = ""
    for job in _job_cache:
        if job["application_url"] == application_url:
            posting_date = job["posting_date"]
            break

    return description, posting_date
