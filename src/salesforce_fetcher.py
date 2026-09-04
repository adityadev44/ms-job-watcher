"""Fetches Salesforce job listings via Salesforce's own static careers JSON feed.

Salesforce's ATS backend is Workday (`salesforce.wd12.myworkdayjobs.com/
External_Career_Site`), same family as ~25 other companies already in this
repo — but the public careers site (`salesforce.com/company/careers/jobs`)
does NOT call the Workday CXS search API directly. Instead its frontend
fetches two static, unauthenticated JSON files from Salesforce's own CDN:

    https://a.sfdcstatic.com/digital/xsf/careers/prod/jobs_1.json
    https://a.sfdcstatic.com/digital/xsf/careers/prod/jobs_2.json

Both report the identical ~1479 global postings (same `Job_Requisition_Ref_ID`
set, same `Total_Jobs`/`Count`), but jobs_1.json's `Job_Description` field is
empty for all but a handful of entries while jobs_2.json (despite being ~4x
the byte size for the *same* job count) has the full HTML description filled
in for every single job. Only jobs_2.json is fetched here — jobs_1.json adds
nothing this fetcher needs.

No keyword or location filtering exists server-side (it's a static file, not
a search API) — the whole feed is downloaded once per process and cached;
India postings are filtered client-side via the job's own `Countries` list
(exact match "India", not a substring, so no Indiana/Indianapolis risk).
`Job_Requisition_Primary_Location` is already formatted like
"India - Hyderabad", which already contains the literal string "India", so
no client-side ", India" append is needed for matcher.py's `is_india_job()`.

`External_Job_Posting_Start_Date` is already `YYYY-MM-DD` — no relative-date
parsing needed. `External_Job_Posting_Site` is a real, public, unauthenticated
Workday External_Career_Site URL (verified via a live HTTP 200) — used
directly as `application_url`.

Because the full HTML description is already present in the same JSON blob
used for the search pass, `fetch_job_description` is a pure in-module cache
lookup — no second HTTP request per job, ever.
"""

from __future__ import annotations

import html as html_mod
import re
import time

import requests

_FEED_URL = "https://a.sfdcstatic.com/digital/xsf/careers/prod/jobs_2.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.salesforce.com/company/careers/jobs/",
}

# Module-level cache: the full feed is fetched once per process and reused
# for every keyword/location combination matcher.py drives us through.
_india_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure during the feed fetch."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _fill_cache(timeout: int = 30) -> None:
    """Download the full global feed once and cache India postings.

    ``_cache_filled`` is set before the request completes (Honeywell lesson
    from PLAYBOOK.md) so a failure doesn't trigger a retry storm on every
    subsequent keyword/location call within the same process — the next
    scheduled run (a fresh process) gets a clean retry.
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_FEED_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Salesforce: 429 rate-limited fetching jobs_2.json")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Salesforce feed fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Salesforce feed fetch: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Salesforce feed returned non-JSON body: {exc}") from exc

    entries = payload.get("Report_Entry", [])
    collected: list[dict] = []
    for entry in entries:
        countries = entry.get("Countries") or []
        if "India" not in countries:
            continue
        job_id = entry.get("Job_Requisition_Ref_ID")
        title = entry.get("Job_Posting_Title")
        location = entry.get("Job_Requisition_Primary_Location") or "India"
        application_url = entry.get("External_Job_Posting_Site")
        posting_date = entry.get("External_Job_Posting_Start_Date") or ""
        if not (job_id and title and application_url):
            continue
        description = _strip_html(entry.get("Job_Description", ""))
        collected.append({
            "id": str(job_id),
            "title": title,
            "location": location,
            "posting_date": posting_date,
            "application_url": application_url,
        })
        _desc_cache[application_url] = (description, posting_date)

    _india_cache.clear()
    _india_cache.extend(collected)
    print(f"[Salesforce] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Salesforce India jobs.

    Keywords and location are ignored — the feed is a static export with no
    server-side search, so every call returns a slice of the same cached
    India pool; matcher.py's own title/skill filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Salesforce job.

    Both values are already in the cache built by ``_fill_cache`` (the same
    JSON blob search results came from carries the full HTML description) —
    no second HTTP request is made in the normal case.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Unexpected miss (e.g. called before any fetch_jobs in this process,
    # or a job that dropped out of the feed between calls): force a fresh
    # cache fill and retry once before giving up.
    global _cache_filled
    _cache_filled = False
    _fill_cache(timeout=timeout)
    return _desc_cache.get(application_url, ("", ""))
