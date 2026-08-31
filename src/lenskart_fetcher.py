"""Fetches Lenskart job listings via their "ainterviews.com" custom job-board API.

ATS discovery: Lenskart's advertised careers URL, hiring.lenskart.com/jobs,
HTTP 301-redirects to https://ainterviews.com/job_board/lenskart_ho/ — a
white-labelled job board hosted on a third-party ATS called "ainterviews.com"
(not one of the vendors in the PLAYBOOK's Common ATS table: not Workday,
SuccessFactors, Oracle HCM CE, Greenhouse, Lever, iCIMS, Taleo, or Darwinbox).
The board page is a small client-rendered SPA (Tailwind + vanilla JS) that
calls two plain JSON endpoints with no auth/session/CSRF required, verified
live 2026-08-31:
  - GET https://ainterviews.com/api/job_board/lenskart_ho/           board metadata
  - GET https://ainterviews.com/api/job_board/lenskart_ho/jobs/      full job list
The jobs endpoint returns the entire board (~67 postings across all
departments) in one call, each with a full HTML `description` field already
inline — no separate per-job detail fetch is needed, so the whole pool is
cached once per process (same "cache-once" idiom as Razorpay/CRED/Groww/
Meesho/Paytm). The frontend JS itself never sends query params — it fetches
everything and filters client-side in the browser — confirming keyword/
location are safe to ignore server-side, even though the endpoint happens to
also accept (undocumented) `search=`/`location=` params that narrow results;
relying on the always-available unfiltered fetch is simpler and more robust.

Field mapping: `id` (int) -> str id; `title` -> title; `location` -> location
(see quirk below); `posted_date` (ISO 8601, e.g.
"2026-06-17T10:52:36.005484+00:00") -> first 10 chars for YYYY-MM-DD;
`apply_url` is a path relative to ainterviews.com (e.g.
"/job_board/lenskart_ho/job/176/") -> prefixed with the domain to build the
absolute application_url (verified renders a real job detail page, HTTP 200).

Location quirk: most India postings use a bare city name with no "India"
word at all ("Bangalore", "Gurugram", "Delhi", "Bhiwadi", "Okhla, Delhi") —
matcher.py's is_india_job() requires a literal "india" substring, so these
would be silently dropped unmodified. Following the Razorpay/CRED/Invesco
convention: ", India" is appended only when a recognised India city token is
found in the location text (and "india" isn't already present) — genuine
non-India office locations seen on this board (Spain, Singapore, UAE, Japan,
Thailand, Italy) are left untouched so is_india_job() correctly rejects them
rather than being blindly relabelled.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_SLUG = "lenskart_ho"
_API_BASE = f"https://ainterviews.com/api/job_board/{_BOARD_SLUG}"
_JOBS_URL = f"{_API_BASE}/jobs/"
_JOB_DETAIL_BASE = f"https://ainterviews.com/job_board/{_BOARD_SLUG}/job"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://ainterviews.com/job_board/lenskart_ho/",
}

# India city/region tokens observed (or plausible) on this board — office
# locations are frequently city-only with no country word. Used to normalise
# recognised India locations to include ", India" without blindly trusting
# every location string (some postings are genuinely Spain/Singapore/UAE/
# Japan/Thailand/Italy).
_INDIA_CITIES = (
    "bangalore", "bengaluru", "delhi", "gurugram", "gurgaon", "bhiwadi",
    "okhla", "ncr", "noida", "hyderabad", "mumbai", "pune", "chennai",
    "kolkata", "jaipur", "chandigarh", "kochi", "trivandrum", "rajasthan",
)

# Module-level cache: the full board is fetched once and reused for every
# keyword/page call, same idiom as razorpay_fetcher.py / cred_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _is_india_location(loc: str) -> bool:
    low = loc.lower()
    return any(city in low for city in _INDIA_CITIES)


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _normalize_location(raw_loc: str) -> str:
    loc = (raw_loc or "").strip()
    if loc and "india" not in loc.lower() and _is_india_location(loc):
        return f"{loc}, India"
    return loc


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Lenskart (ainterviews.com) board once and cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_JOBS_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Lenskart: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Lenskart cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Lenskart cache fill: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Lenskart cache fill: invalid JSON — {exc}") from exc

    raw_jobs = payload.get("jobs") or []
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "").strip()
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc = _normalize_location(j.get("location") or "")

        posted_date = j.get("posted_date") or ""
        posting_date = posted_date[:10] if posted_date else ""

        apply_path = j.get("apply_url") or f"/job_board/{_BOARD_SLUG}/job/{job_id}/"
        if apply_path.startswith("http"):
            app_url = apply_path
        else:
            app_url = f"https://ainterviews.com{apply_path}"

        _desc_cache[job_id] = j.get("description") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache[:] = collected
    print(f"[Lenskart] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Lenskart jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    the board's own frontend fetches the full ~67-job pool unfiltered and
    does all filtering client-side in the browser, so there is no reliable
    server-side keyword/location contract to depend on. All jobs (India and
    non-India) are returned — India scoping is left to matcher.py's
    is_india_job() / config exclude_locations, aside from the city-name
    normalisation documented in the module docstring.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    """application_url is .../job/{id}/ — take the last non-empty segment."""
    path = (application_url or "").split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Lenskart job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    the board's jobs response already includes each job's full HTML
    description, so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = _job_id_from_url(application_url)
    description = _strip_html(_desc_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
