"""
Databricks job fetcher — Greenhouse ATS public REST API.

Databricks' careers page embeds a Greenhouse job board (board token
`databricks`); the public boards API is directly reachable with no auth
and no bot wall:

    GET https://boards-api.greenhouse.io/v1/boards/databricks/jobs?content=true

Confirmed live (2026-09-06): HTTP 200, 870 total postings globally, each
with a full HTML `content` field already inline in the same response — no
separate per-job detail fetch needed (same "cache-once" pattern as
AlphaSense/Stripe/Razorpay/Groww/Paytm, the other Greenhouse boards
already in this repo). Query params (`q=`/`search=`) are NOT applied
server-side — confirmed identical job count with and without a nonsense
`q=` param — so the full pool is fetched once and cached in-module
(`_cache_filled` set *before* the fetch attempt, so a transient failure
doesn't retry-storm every subsequent call — the Honeywell lesson).

There is no separate posting-date field; `updated_at` (truncated to
YYYY-MM-DD) is used as `posting_date`, per Greenhouse's standard shape.

URL quirk: like Stripe's board (unlike most Greenhouse boards in this
repo), Databricks' `absolute_url` is query-string based
(`https://databricks.com/company/careers/open-positions/job?gh_jid=<id>`)
— `_extract_job_id` parses `gh_jid` explicitly rather than assuming the
last path segment is the id.

Location: unlike Stripe's messy Bengaluru/Bangalore spellings, Databricks'
`location.name` field already says "Bengaluru, India" / "Mumbai, India" /
"Pune, India" / "Delhi, India" / plain "India" / "Remote - India" for
every India posting — no city normalisation needed here.

**Gotcha (not fixed here — matcher.py is out of scope for this fetcher):**
one real posting uses the joint location string "Chicago, Illinois;
Indiana" (a US req, Illinois + the US state of Indiana). matcher.py's
`is_india_job()` does a bare `"india" in location.lower()` substring check,
and "Indiana" contains "india" as a literal substring — that one posting
would spuriously pass the India check. Harmless in practice today: the
title ("Strategic Enterprise Account Executive, Life Sciences") doesn't
pass `title_family` either way, so it can't produce a false-positive
alert, but it's a genuine latent false-positive path (same class of
issue as Cisco/Micron facet-leakage, just triggered by a US state name
instead of a bad facet) — flagged for the coordinator, not silently
patched, since fixing the substring check is a matcher.py-wide change out
of scope for this fetcher.

Live-verified 2026-09-06 totals: 95 of 870 global postings are India
(Bengaluru/Mumbai/Pune/Delhi/Remote-India/plain "India", excluding the one
"Indiana" false positive above). Of those, 41 pass the standard keyword
list — all in the AI/ML/Python-adjacent "software engineer" track (Senior/
Staff/Sr Software Engineer across many teams, plus one literal "AI
Engineer - FDE"); zero .NET/C# matches (expected — Databricks doesn't run
a .NET stack). This is Databricks' real India engineering hub (Bengaluru),
not a GCC/ops-only presence — titles include "Staff Software Engineer -
Machine Learning (Search)", "Senior Software Engineer - Data + AI
Observability", etc., genuine backend/ML engineering work.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from urllib.parse import parse_qs, urlparse

import requests

_BOARD_TOKEN = "databricks"
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


def _extract_job_id(application_url: str) -> str:
    """Databricks' Greenhouse `absolute_url` is query-string based
    (`https://databricks.com/company/careers/open-positions/job?gh_jid=<id>`),
    not the usual path-style `/jobs/<id>` — parse `gh_jid` explicitly,
    falling back to the last path segment for any other shape."""
    qs = parse_qs(urlparse(application_url).query)
    if "gh_jid" in qs and qs["gh_jid"]:
        return qs["gh_jid"][0]
    return application_url.rstrip("/").split("/")[-1]


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Databricks Greenhouse board once and cache it.

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
                raise RateLimitError("Databricks: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Databricks cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Databricks cache fill: no response — {last_exc}")

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
    print(f"[Databricks] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Databricks jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full board regardless
    of query params. All jobs (India and non-India) are returned — India
    scoping is left entirely to matcher.py's is_india_job() / config
    exclude_locations (see module docstring for the one known "Indiana"
    substring false-positive this implies).
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Databricks job.

    Served entirely from the cache filled by _fill_cache() — Greenhouse's
    search response already includes the full HTML `content` field for
    every job, so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = _extract_job_id(application_url)
    description = _strip_html(_content_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
