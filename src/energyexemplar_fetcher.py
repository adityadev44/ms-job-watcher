"""
Energy Exemplar (India) job fetcher — Greenhouse ATS REST API.

Verified from scratch (2026-09-04) via DevTools/direct API probing, per the
playbook's standing "don't trust a secondary-research ATS label" rule — no
prior guess was assumed. Energy Exemplar's public careers page
(energyexemplar.com/careers/) links out to a Greenhouse job board at
`job-boards.greenhouse.io/energyexemplarllc`. Public boards API:

GET https://boards-api.greenhouse.io/v1/boards/energyexemplarllc/jobs?content=true

returns the entire GLOBAL board (38 postings as of verification — US/LatAm/
EMEA/APAC roles included, not an India-only board like Definitive Healthcare's)
in one call, including each job's full HTML `content` field — no per-job
detail fetch needed. Same ignores-query-params, fetch-once-cache pattern as
every other Greenhouse board in this repo (Groww, Razorpay, CRED, Meesho,
Paytm, Definitive Healthcare): keyword/location are not applied server-side,
so the full pool is fetched once and cached in-module. `_cache_filled` is set
*before* the fetch attempt (the Honeywell lesson) so a transient failure
doesn't retry-storm on every subsequent call within the same process.

There is no separate posting-date field; `updated_at` (truncated to
YYYY-MM-DD) is used as `posting_date`, per Greenhouse's standard shape.

Content quirk: this board's `content` field is single-level HTML-entity
escaped (confirmed by direct inspection — NOT the double-escaping seen on
Groww/Razorpay/Definitive Healthcare's boards). `_strip_html` still
unescapes-strip-unescapes for safety/consistency with every other Greenhouse
fetcher in this repo; a second unescape pass is a no-op on already-clean
text, so the shared approach is safe regardless of which encoding depth a
given Greenhouse tenant happens to use.

NEW location quirk not previously seen on this repo's other Greenhouse boards
(Groww/Razorpay/CRED/Meesho/Paytm/Definitive Healthcare are all single-city
per posting): Energy Exemplar's India engineering reqs are jointly posted
across two cities in ONE semicolon-separated location string, e.g.
"Bengaluru, Karnataka, India; Pune, Maharashtra, India" (this is exactly the
shape of the user-flagged "Senior Software Engineer -Backend(.NET)" role,
job id 4982436008). Passed through verbatim, matcher.py's `exclude_locations`
check (a plain substring test — see PLAYBOOK "Filter Layers") would silently
drop this job entirely because the string also contains "Pune", even though
Bengaluru — a genuinely valid, non-excluded location — is equally offered on
the same requisition. `_pick_display_location()` fixes this: for a
multi-location string, it prefers a segment that does NOT contain any of the
config's standard excluded-city tokens (Chennai/Tamil Nadu/Pune/Chandigarh/
Kochi/Kerala/Trivandrum/Lucknow/Nagpur/Madurai/Kolkata/Indore, hardcoded here
to mirror config.yaml's `_defaults.exclude_locations` — this fetcher has no
access to per-run config) and does contain "india". If every segment is an
excluded city (not currently observed on this board, but possible on a
future posting), the original full string is kept unmodified so the job is
still correctly excluded rather than silently mis-included.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_TOKEN = "energyexemplarllc"
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

# Mirrors config.yaml's `_defaults.exclude_locations` anchor. Duplicated here
# (not imported) because fetchers are self-contained modules with no access
# to per-run config — see module docstring for why this list is needed at
# fetch time rather than left entirely to matcher.py.
_EXCLUDED_CITY_TOKENS = [
    "chennai", "tamil nadu", "pune", "chandigarh", "kochi", "kerala",
    "trivandrum", "lucknow", "nagpur", "madurai", "kolkata", "indore",
]

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


def _pick_display_location(raw_loc: str) -> str:
    """Return the location string to hand to matcher.py.

    A single-location string is returned unchanged. A multi-location string
    (Greenhouse separates joint postings with "; ") is reduced to the first
    segment that mentions "india" and none of the standard excluded-city
    tokens, so a job jointly posted in a valid city (e.g. Bengaluru) and an
    excluded one (e.g. Pune) isn't dropped by exclude_locations' substring
    check purely because the excluded city is *also* listed. If no segment
    qualifies, the original string is returned as-is (every offered city is
    excluded, so correctly excluding the job is the right outcome).
    """
    if ";" not in raw_loc:
        return raw_loc
    segments = [seg.strip() for seg in raw_loc.split(";") if seg.strip()]
    for seg in segments:
        seg_lower = seg.lower()
        if "india" in seg_lower and not any(tok in seg_lower for tok in _EXCLUDED_CITY_TOKENS):
            return seg
    return raw_loc


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Energy Exemplar global Greenhouse board once and cache it.

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
                    "Energy Exemplar: 429 rate-limited during cache fill"
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
                f"Energy Exemplar cache fill failed: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(
            f"Energy Exemplar cache fill: no response — {last_exc}"
        )

    raw_jobs = r.json().get("jobs", [])
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        raw_loc = ((j.get("location") or {}).get("name") or "").strip()
        loc = _pick_display_location(raw_loc)

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
    print(f"[Energy Exemplar] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Energy Exemplar jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full board regardless of
    query params. All jobs (global, not just India) are returned — India
    scoping is left to matcher.py's is_india_job() / exclude_locations, same
    discipline as every other cache-once fetcher here.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Energy Exemplar job.

    Served entirely from the cache filled by _fill_cache() — Greenhouse's
    search response already includes the full HTML `content` field for every
    job, so no separate detail HTTP call is made.
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
