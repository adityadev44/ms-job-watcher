"""
Zimmer Biomet (India GCC) job fetcher — Phenom People (careers.zimmerbiomet.com).

ATS recon (2026-10-05):
  `careers.zimmerbiomet.com/us/en/search-results` embeds the full search
  response server-side as a `phApp.ddo = {...}` JSON blob — the identical
  Phenom People pattern already proven for GE HealthCare and United Airlines
  in this repo. No Playwright needed; plain `requests` reads both the search
  page and the description stub (see below).

Search: `GET /us/en/search-results?keywords=...&from=...`
  - Page size is 10 server-side (fixed). `from=` is a clean offset — clean
    break at end (empty `jobs` list, HTTP 200, no wraparound).
  - Location/country query params do NOT filter server-side — confirmed by
    comparison: `country=India` returns the same global result set. Each
    job's own `country` field is a clean, always-populated exact value, so
    fetcher filters by `country == "India"` client-side.
  - Keywords may narrow the pool server-side (same family as GE HealthCare),
    so the complete India pool is cached per keyword on first call.

Description: Zimmer Biomet's real hiring backend is SAP SuccessFactors
  (`career8.successfactors.com/careers?company=zimmerin01&...`), NOT
  Workday. The SuccessFactors job-detail page sets `loginFlowRequired=true`
  — plain HTTP scraping returns a login redirect (186 KB redirect shell),
  not job content. The search response already carries a `descriptionTeaser`
  (~200 chars) per job. This is used as the canonical description: it is
  enough for keyword/skill matching and is cached in-module during the
  initial search walk, so `fetch_job_description` makes NO extra HTTP calls.
  `applyUrl` from the search response is the direct SuccessFactors apply
  link and is used as `application_url` (correct, real, clickable).
"""
from __future__ import annotations

import json
import re
import time

import requests

_BASE_URL = "https://careers.zimmerbiomet.com"
_SEARCH_URL = f"{_BASE_URL}/us/en/search-results"
_DDO_MARKER = "phApp.ddo = {"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_RAW_PAGES = 120
_PAGE_DELAY = 0.15

# keyword -> fully-walked, India-filtered job dicts
_keyword_cache: dict[str, list[dict]] = {}
# job_id -> descriptionTeaser (populated during search walk)
_teaser_cache: dict[str, str] = {}


class RateLimitError(Exception):
    pass


def _extract_ddo(html: str) -> dict | None:
    idx = html.find(_DDO_MARKER)
    if idx == -1:
        return None
    start = idx + len(_DDO_MARKER) - 1
    depth = 0
    in_str = False
    esc = False
    quote = None
    i = start
    n = len(html)
    while i < n:
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        else:
            if c in "\"'":
                in_str = True
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1
    try:
        return json.loads(html[start:i])
    except ValueError:
        return None


def _fetch_raw_page(keyword: str, from_: int, timeout: int) -> list[dict]:
    params = {"keywords": keyword, "from": from_}
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    f"Zimmer Biomet search: 429 rate-limited (keyword={keyword!r})"
                )
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
                f"Zimmer Biomet search failed (keyword={keyword!r}): {exc}"
            ) from exc
    if r is None:
        raise RateLimitError(f"Zimmer Biomet search: no response — {last_exc}")
    data = _extract_ddo(r.text)
    if not data:
        return []
    erf = data.get("eagerLoadRefineSearch", {}) or {}
    return erf.get("data", {}).get("jobs", []) or []


def _search(keyword: str, timeout: int) -> list[dict]:
    raw_jobs: list[dict] = []
    from_ = 0
    for page_num in range(_MAX_RAW_PAGES):
        if page_num > 0:
            time.sleep(_PAGE_DELAY)
        batch = _fetch_raw_page(keyword, from_, timeout)
        if not batch:
            break
        raw_jobs.extend(batch)
        from_ += len(batch)

    jobs: list[dict] = []
    for j in raw_jobs:
        country = (j.get("country") or "").strip()
        if country.lower() != "india":
            continue
        job_id = str(j.get("jobId") or j.get("reqId") or "")
        apply_url = j.get("applyUrl") or ""
        if not job_id or not apply_url:
            continue
        location = (
            j.get("cityStateCountry")
            or j.get("location")
            or f"{j.get('city', '')}, {country}".strip(", ")
        )
        posting_date = (j.get("postedDate") or j.get("dateCreated") or "")[:10]
        teaser = (j.get("descriptionTeaser") or "").strip()
        _teaser_cache[job_id] = teaser
        jobs.append({
            "id": job_id,
            "title": (j.get("title") or "").strip(),
            "location": location,
            "posting_date": posting_date,
            "application_url": apply_url,
        })
    return jobs


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of Zimmer Biomet's India job results for *keyword*.

    `location` is accepted for interface compatibility but ignored — Zimmer
    Biomet's Phenom People location params do not filter server-side.
    """
    key = keyword or ""
    if key not in _keyword_cache:
        _keyword_cache[key] = _search(key, timeout)
    return _keyword_cache[key][start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Zimmer Biomet job.

    Description is served from the `descriptionTeaser` cached during the
    search walk — the SuccessFactors backend requires login for the full
    description page, so no extra HTTP call is made. posting_date is empty
    (matcher.py leaves the already-good date from the search response alone).
    """
    job_id = re.search(r"career_job_req_id=(\d+)", application_url)
    if job_id:
        return _teaser_cache.get(job_id.group(1), ""), ""
    # Fall back to any job_id that matches a simple numeric suffix.
    for key, teaser in _teaser_cache.items():
        if key in application_url:
            return teaser, ""
    return "", ""
