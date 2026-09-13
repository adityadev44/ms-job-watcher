"""Fetches Zscaler job listings via the Greenhouse ATS.

Zscaler's careers page (zscaler.com/careers) is Greenhouse-backed, board
token "zscaler". Confirmed live 2026-09-13:

    GET https://boards-api.greenhouse.io/v1/boards/zscaler/jobs?content=true

-- HTTP 200, ~371 total postings globally. Same "cache-once, ignores
keywords" family as MongoDB/Okta/Glean/Groww/Razorpay/AlphaSense: no
server-side keyword/location query param exists, the whole board is
returned in one call, so it is fetched once and cached in-module.

Location quirk -- a NEW shape not previously documented in this repo:
`location.name` uses the bare 3-letter ISO country abbreviation "IND"
(e.g. "Bangalore, IND", "Gurgaon, IND; Mumbai, IND"), not the word "India"
at all. A naive substring check for "india" would only catch the one
posting whose location literally says "Remote - India" and would ALSO
false-positive on "Remote - Indiana, USA" (the classic Indianapolis/Indiana
trap already documented for PayPal/FactSet/J&J/Roche) since "indiana"
contains "india" as a substring too. Fixed by normalizing with a
word-boundary regex that only matches the literal 3-letter token "IND"
(never matches inside "Indiana" or "India" itself, since a real word
boundary sits on both sides only for the standalone abbreviation) and
replacing it with "India".

Verified live 2026-09-13: 76 genuine India postings once normalized (of
371 global), heavily Bangalore-based with real Mohali/Gurgaon/Hyderabad/
Mumbai/Pune presence. Very strong genuine engineering signal: "Principal
Software Development Engineer - Rust", "Senior Machine Learning Engineer
(Agentic AI)", "Staff Software Development Engineer - DevOps", "Sr. Staff
Software Development Engineer - Golang + Control Plane", "Senior Fullstack
Engineer (Java + React.js)", multiple "Staff/Sr. Staff Site Reliability
Engineer" reqs.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "zscaler"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL_TMPL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.zscaler.com/careers",
}

# Matches the standalone 3-letter country-code token "IND" (Zscaler's own
# location shorthand) without ever matching inside "India"/"Indiana"/
# "Indianapolis" -- \b on both sides requires non-word chars immediately
# before AND after, which "IND" as a whole comma/semicolon-delimited token
# always has, but a substring inside a longer word never does.
_IND_TOKEN_RE = re.compile(r"\bIND\b")

_india_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Greenhouse."""


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(_html_mod.unescape(raw or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_date(job: dict) -> str:
    raw = job.get("first_published") or job.get("updated_at") or ""
    return raw[:10] if raw else ""


def _normalize_location(raw_loc: str) -> str:
    """Replace the bare 'IND' country-code token with 'India'."""
    loc = (raw_loc or "").strip()
    if not loc:
        return loc
    if "india" in loc.lower():
        return loc
    return _IND_TOKEN_RE.sub("India", loc)


def _get_with_retry(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Zscaler {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zscaler {what} failed: {exc}") from exc
    raise RateLimitError(f"Zscaler {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = _get_with_retry(f"{_LIST_URL}?content=true", timeout, "cache fill")
    raw_jobs = r.json().get("jobs", [])

    collected: list[dict] = []
    for job in raw_jobs:
        job_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc_name = ((job.get("location") or {}).get("name") or "").strip()
        loc_norm = _normalize_location(loc_name)
        if "india" not in loc_norm.lower():
            continue

        app_url = job.get("absolute_url") or f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs/{job_id}"

        _content_cache[job_id] = job.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_norm,
            "posting_date": _parse_date(job),
            "application_url": app_url,
        })

    _india_cache[:] = collected
    print(f"[Zscaler] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Zscaler India jobs.

    Greenhouse's public job-list endpoint has no keyword/location query
    param and always returns the full current board, so the pool is
    fetched once and cached; keyword/location arguments are accepted for
    interface compatibility only.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start: start + num]


def _job_id_from_url(application_url: str) -> str:
    url = application_url or ""
    m = re.search(r"/jobs/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]gh_jid=(\d+)", url)
    return m.group(1) if m else ""


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    job_id = _job_id_from_url(application_url)

    if job_id and job_id in _content_cache:
        return _strip_html(_content_cache[job_id]), ""

    if not job_id:
        raise RateLimitError(f"Zscaler description: could not parse job id from {application_url!r}")

    r = _get_with_retry(_DETAIL_URL_TMPL.format(job_id=job_id) + "?content=true", timeout, "description fetch")
    job = r.json()
    description = _strip_html(job.get("content") or "")
    posting_date = _parse_date(job)
    _content_cache[job_id] = job.get("content") or ""
    return description, posting_date
