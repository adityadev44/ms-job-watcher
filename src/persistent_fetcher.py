"""Fetches Persistent Systems job listings via the Zwayam ATS.

careers.persistent.com is an Angular SPA backed by Zwayam
(apipersistent.zwayam.com), a widely-used Indian recruiting platform.

Key quirks:
- POST /jobs/search takes a multipart form with a JSON `filterCri` field
  (paginationStartNo, sortCriteria, anyOfTheseWords). The server hard-caps
  pagination at 9 results per page regardless of any page-size override, and
  `anyOfTheseWords` does a loose OR-of-words match that's too noisy to rely
  on — so all ~700 global postings are paginated once (~78 requests) and
  cached in-module; India ones (~650) are kept and re-served from cache for
  every keyword call.
- The `location` field on search results is just "India" (no city) for
  Indian postings, which would let Pune/Chennai roles slip past
  exclude_locations. The `jobUrl` slug reliably embeds the city
  (e.g. "programmer-dev-india-pune-2026060717164612") — parsed out via
  regex and appended so exclude_locations works correctly.
- Full description lives behind a separate POST to
  /jobs-service/v1/jobs/careersite with {jobUrl, companyId} — the search
  response's own description fields are empty. Detail page URL pattern is
  https://careers.persistent.com/jobview/{jobUrl} (found in the Angular
  bundle's route table — most guessed URL patterns 200 but silently fall
  back to the homepage shell without an API call).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests

_CAREERS_BASE = "https://careers.persistent.com"
_API_BASE = "https://apipersistent.zwayam.com"
_SEARCH_URL = f"{_API_BASE}/jobs/search"
_DETAIL_URL = f"{_API_BASE}/jobs-service/v1/jobs/careersite"
_COMPANY_ID_B64 = "MTQ5Nzc="  # base64("14977")
_COMPANY_ID = "14977"
_PAGE_SIZE = 9   # server-enforced; not configurable
_MAX_PAGES = 150  # safety ceiling (~1350 jobs)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_CAREERS_BASE}/",
    "Origin": _CAREERS_BASE,
}

_CITY_RE = re.compile(r"india-([a-z]+)-\d+$")

# Module-level cache: filled once, reused for all keyword calls.
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-Mon-YYYY' (e.g. '07-Jul-2022') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _location_from_job(job_url: str) -> str:
    m = _CITY_RE.search(job_url or "")
    if m:
        return f"{m.group(1).title()}, India"
    return "India"


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through every Persistent posting once and cache India ones.

    _cache_filled is set before the loop so a failure doesn't trigger a
    retry storm on every subsequent keyword call (Honeywell lesson).
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    start = 0
    for page_num in range(_MAX_PAGES):
        if page_num > 0:
            time.sleep(0.15)

        filter_cri = {
            "paginationStartNo": start,
            "selectedCall": "sort",
            "sortCriteria": {"name": "modifiedDate", "isAscending": False},
            "anyOfTheseWords": "",
        }
        files = {
            "filterCri": (None, json.dumps(filter_cri)),
            "domain": (None, "careers.persistent.com"),
            "companyId": (None, _COMPANY_ID_B64),
        }

        last_exc: Exception | None = None
        r = None
        for attempt in range(3):
            try:
                r = requests.post(_SEARCH_URL, headers=_HEADERS, files=files, timeout=timeout)
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Persistent: 429 rate-limited during cache fill")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Persistent cache fill failed: {exc}") from exc

        if r is None:
            raise RateLimitError(f"Persistent cache fill: no response — {last_exc}")

        payload = r.json().get("data", {})
        batch = payload.get("data", [])
        if not batch:
            break

        for item in batch:
            src = item.get("_source", {})
            if (src.get("location") or "").strip() != "India":
                continue
            job_id = src.get("id")
            title = (src.get("jobTitle") or "").strip()
            job_url = src.get("jobUrl") or ""
            if not (job_id and title and job_url):
                continue
            collected.append({
                "id": str(job_id),
                "title": title,
                "location": _location_from_job(job_url),
                "posting_date": _parse_date(src.get("createDate") or ""),
                "application_url": f"{_CAREERS_BASE}/jobview/{job_url}",
            })

        start += len(batch)
        if not payload.get("hasMoreData"):
            break

    _india_cache = collected
    print(f"[Persistent] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Persistent Systems India jobs.

    Keywords are ignored — Zwayam's `anyOfTheseWords` is a noisy OR-match
    and unreliable for narrowing; the shared title/skill filters in
    matcher.py do the real work. The full India pool is cached once.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single Persistent job."""
    job_url = application_url.rsplit("/jobview/", 1)[-1]

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(
                _DETAIL_URL,
                headers={**_HEADERS, "Content-Type": "application/json"},
                json={
                    "jobUrl": job_url,
                    "externalSource": "CareerSite",
                    "campusUrl": "empty",
                    "companyId": _COMPANY_ID,
                },
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Persistent description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Persistent description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Persistent description fetch: no response — {last_exc}")

    data = r.json()
    description = _strip_html(data.get("longDescription", ""))
    posting_date = _parse_date(data.get("createDate", "") or "")
    return description, posting_date
