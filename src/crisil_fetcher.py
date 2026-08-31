"""Fetches CRISIL (S&P Global subsidiary) job listings via the Zwayam ATS.

career.crisil.com is an Angular SPA backed by Zwayam's shared multi-tenant
endpoint (public.zwayam.com — not a company-specific subdomain like
apipersistent.zwayam.com). The tenant is identified purely by a companyId
value embedded in the frontend bundle. Same ATS family as Persistent Systems
(persistent_fetcher.py) and eClerx, with a few CRISIL-specific differences:

Key quirks:
- Discovered via the Angular bundle (main.*.js): `APIENDPOINT` /
  `TENANTAPIURL` both point at the shared `https://public.zwayam.com/`
  endpoint (confirmed live — the site's own "Powered by" footer links to
  zwayam.com). `COMPANYID` is base64 `"MTU0Mzg="` -> `"15438"`.
- POST /jobs/search takes a multipart form with a JSON `filterCri` field
  (paginationStartNo, sortCriteria, anyOfTheseWords) plus `domain` and
  `companyId` (base64-encoded) fields — identical shape to Persistent.
  Server-enforced page size is 10 (`facetedSearchConfig.paginationHowMuch`),
  confirmed live; NOT configurable via any request parameter.
- Unlike Persistent, CRISIL's `anyOfTheseWords` DOES narrow results
  server-side (e.g. "engineer" -> 56/649). It is still not used here: caching
  the full ~650-job pool once and letting matcher.py's title/skill filters do
  the real work is simpler, avoids any keyword-matching surprises, and
  matches the documented Zwayam pattern in PLAYBOOK.md.
- The search response's own `mediumDescriptionWithoutHtml` field is empty for
  most postings (populated for only a couple observed) — descriptions are
  fetched from the separate detail endpoint for every job, same as
  Persistent, not read inline.
- Full description lives behind POST /jobs-service/v1/jobs/careersite with
  {jobUrl, externalSource: "CAREERSITE", campusUrl: "empty", companyId}
  (companyId here is the *decoded* "15438", not the base64 form used in the
  search request). Detail page URL pattern is
  https://career.crisil.com/crisil/jobview/{jobUrl} (Angular route table:
  `path: "jobview/:jobUrl"`, `<base href="/crisil/">`).
- `locationSeparatedbySlash` already includes ", India" for every genuine
  India posting (e.g. "Mumbai, Maharashtra, India") — no manual appending
  needed. Non-India jobs (US/UK/Canada roles CRISIL also posts on this same
  board) have `locationSeparatedbySlash: None` and a bare `location` like
  "UK" or "New Jersey, USA" with no "India" substring, so they're dropped
  naturally by matcher.py's `is_india_job()` / the client-side India filter
  applied in this module.
- Many real India tech postings list multiple cities (e.g.
  "Mumbai, Maharashtra, India / Pune, Maharashtra, India") joined from a
  structured `jobLocationRecord` list. Since Pune is in the global
  `exclude_locations` substring check, a Mumbai+Pune combo posting is
  currently excluded entirely even though the Mumbai option alone would
  qualify — a known tradeoff of the shared substring-based exclude check
  (not fixed here; see PLAYBOOK's exclude_terms substring-matching
  discussion for the same class of issue).
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime
from json import dumps as _json_dumps

import requests

_CAREERS_BASE = "https://career.crisil.com/crisil"
_API_BASE = "https://public.zwayam.com"
_SEARCH_URL = f"{_API_BASE}/jobs/search"
_DETAIL_URL = f"{_API_BASE}/jobs-service/v1/jobs/careersite"
_COMPANY_ID_B64 = "MTU0Mzg="  # base64("15438")
_COMPANY_ID = "15438"
_MAX_PAGES = 200  # safety ceiling; server page size is a fixed 10 (~2000 jobs)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_CAREERS_BASE}/",
    "Origin": "https://career.crisil.com",
}

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
    """Convert 'DD-Mon-YYYY' (e.g. '05-Aug-2026') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _location_from_job(src: dict) -> str:
    loc = src.get("locationSeparatedbySlash")
    if loc:
        return loc
    records = src.get("jobLocationRecord") or []
    formatted = [r.get("formattedLocation") for r in records if r.get("formattedLocation")]
    if formatted:
        return " / ".join(formatted)
    return (src.get("location") or "").replace(";", " / ")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through every CRISIL posting once and cache India ones.

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
            "filterCri": (None, _json_dumps(filter_cri)),
            "domain": (None, "career.crisil.com"),
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
                    raise RateLimitError("CRISIL: 429 rate-limited during cache fill")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"CRISIL cache fill failed: {exc}") from exc

        if r is None:
            raise RateLimitError(f"CRISIL cache fill: no response — {last_exc}")

        payload = r.json().get("data", {})
        batch = payload.get("data", [])
        if not batch:
            break

        for item in batch:
            src = item.get("_source", {})
            location = _location_from_job(src)
            if "india" not in location.lower():
                continue
            job_id = src.get("id")
            title = (src.get("jobTitle") or "").strip()
            job_url = src.get("jobUrl") or ""
            if not (job_id and title and job_url):
                continue
            collected.append({
                "id": str(job_id),
                "title": title,
                "location": location,
                "posting_date": _parse_date(src.get("createDate") or ""),
                "application_url": f"{_CAREERS_BASE}/jobview/{job_url}",
            })

        start += len(batch)
        if not payload.get("hasMoreData"):
            break

    _india_cache = collected
    print(f"[CRISIL] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of CRISIL India jobs.

    Keywords are ignored — the shared title/skill filters in matcher.py do
    the real work. The full India pool is cached once (see _fill_cache).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single CRISIL job."""
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
                    "externalSource": "CAREERSITE",
                    "campusUrl": "empty",
                    "companyId": _COMPANY_ID,
                },
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("CRISIL description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"CRISIL description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"CRISIL description fetch: no response — {last_exc}")

    data = r.json()
    description = _strip_html(data.get("longDescription", ""))
    posting_date = _parse_date(data.get("createDate", "") or "")
    return description, posting_date
