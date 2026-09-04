"""
Definitive Healthcare (India) job fetcher — Greenhouse ATS REST API.

Verified from scratch (2026-09-04): the India careers page
(https://www.definitivehc.com/about/careers/india) links out to a
dedicated Greenhouse job board, `job-boards.greenhouse.io/definitivehcindia`
(India entity is "Analytical Wizards", part of the Definitive Healthcare
family — the job content itself says so). This is NOT the same board as
Definitive Healthcare's US/global Greenhouse board — board token
`definitivehcindia` is India-only.

Public boards API:
GET https://boards-api.greenhouse.io/v1/boards/definitivehcindia/jobs?content=true
returns the entire board (9 postings as of verification) in one call,
including each job's full HTML `content` field — no per-job detail fetch
needed. Same "ignores query params, fetch-once cache" pattern as every
other Greenhouse board in this repo (Groww, Razorpay, CRED, Meesho,
Paytm): keyword/location are not applied server-side, so the full pool
is fetched once and cached in-module. `_cache_filled` is set *before*
the fetch attempt (the Honeywell lesson) so a transient failure doesn't
retry-storm on every subsequent call within the same process.

There is no separate posting-date field; `updated_at` (truncated to
YYYY-MM-DD) is used as `posting_date`, per Greenhouse's standard shape.

Location quirk: unlike Groww/Razorpay/CRED/Meesho/Paytm (bare city names
with no country word), every job on this board already carries a full
"City, State, India" location string (e.g. "Bengaluru, Karnataka,
India") — the literal "india" substring `matcher.py`'s is_india_job()
needs is already present, so no city-whitelist normalization is done
here. If a future posting on this board ever omits "India" from its
location text, it will simply be filtered out by is_india_job() rather
than silently mis-included — same conservative default used everywhere
else in this repo when a quirk hasn't actually been observed.

Content quirk: this board's `content` field is HTML-entity-escaped one
extra level, same as Groww/Razorpay — the raw string is literally
"&lt;div class=&quot;...&quot;&gt;..." rather than "<div
class=\"...\">...". `_strip_html` unescapes once before stripping tags
(turning entities into real "<...>" markup so the tag regex can match
it) and unescapes again afterward for any inline entities in the actual
text (e.g. "&amp;nbsp;") — a no-op cost on a normally-escaped board, so
it's safe either way.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_TOKEN = "definitivehcindia"
_BOARDS_BASE = "https://boards-api.greenhouse.io/v1/boards"
_JOBS_URL = f"{_BOARDS_BASE}/{_BOARD_TOKEN}/jobs"
_FALLBACK_JOB_BASE = f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Module-level cache: the full board is fetched once and reused for every
# keyword/page call (Greenhouse's public boards API ignores query params).
_job_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Definitive Healthcare India Greenhouse board once and cache it.

    _cache_filled is set to True before the fetch attempt so a failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() /
    fetch_job_description() call within the same process (Honeywell
    lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _JOBS_URL,
                headers=_HEADERS,
                params={"content": "true"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    "Definitive Healthcare: 429 rate-limited during cache fill"
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
                f"Definitive Healthcare cache fill failed: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(
            f"Definitive Healthcare cache fill: no response — {last_exc}"
        )

    raw_jobs = r.json().get("jobs", [])
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc = ((j.get("location") or {}).get("name") or "").strip()

        updated_at = j.get("updated_at") or ""
        posting_date = updated_at[:10] if updated_at else ""

        app_url = j.get("absolute_url") or f"{_FALLBACK_JOB_BASE}/{job_id}"

        _content_cache[job_id] = j.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache = collected
    print(f"[Definitive Healthcare] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Definitive Healthcare India jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full board regardless
    of query params. All jobs are returned (this board is India-only, and
    every observed location string already contains "India" literally) —
    India scoping is still left to matcher.py's is_india_job() /
    exclude_locations, same discipline as every other fetcher here.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Definitive Healthcare job.

    Served entirely from the cache filled by _fill_cache() — Greenhouse's
    search response already includes the full HTML `content` field for
    every job, so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").split("/")[-1]
    description = _strip_html(_content_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
