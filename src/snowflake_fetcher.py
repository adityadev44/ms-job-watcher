"""
Snowflake job fetcher — Ashby public Job Board API.

Snowflake's own careers site (`careers.snowflake.com`) is a Phenom People
front-end, but that layer is just a browse/search UI: every job's real
`applyUrl` in that page's embedded `phApp.ddo` SSR blob points at
`jobs.ashbyhq.com/snowflake/<uuid>` — confirmed live (2026-09-06) by
fetching `careers.snowflake.com/us/en/search-results` with a plain GET and
reading the embedded JSON directly (no JS execution needed to see it).
Ashby is therefore the real backing ATS, and its public, unauthenticated
Job Board API is used directly instead of the Phenom layer:

    GET https://api.ashbyhq.com/posting-api/job-board/snowflake?includeCompensation=true

Confirmed live: HTTP 200, 376 total postings (vs. 380 reported by the
Phenom search-results page for the same instant — a small, expected
delta from timing/internal-only postings, not a fetcher bug). No auth
required for this endpoint; it is Ashby's documented public board API.

**Phenom's own `location=` query param was tested and found NOT usable**:
it's a geocode-radius search (Google Maps-backed), and "India" as a bare
country name fails to geocode (`aboveMaxRadius: true`, empty lat/long),
silently returning 0 jobs even though real India postings exist — this is
why the Ashby API is used directly instead of Phenom's own search endpoint.

Ashby's public board API ignores all query params (confirmed: appending
`&search=zzz` to the URL returns the identical 376-job payload) — the
full pool is fetched once and cached in-module (`_cache_filled` is set
*before* the fetch attempt, so a transient failure doesn't retry-storm on
every subsequent call in the same process — the Honeywell lesson, see
PLAYBOOK "Key Bugs"). `descriptionHtml` is already inline in the same
response for every posting — no separate detail fetch needed.

Location: Ashby's `location` field on this tenant is a mix of shapes —
"IN-Pune", "IN-Bangalore-MSO", "IN-Delhi-Remote" (none contain a literal
"india" substring) and "Bengaluru, India" (already does). All India
postings are additionally confirmed via `address.postalAddress
.addressCountry == "India"` (a clean, reliable field never observed to
disagree with the `location` string), so *that* field — not text
heuristics on `location` — is used to decide India-ness here; the
`location` string itself is normalised to guarantee it contains "India"
(stripping the "IN-" prefix and appending ", India" when not already
present) so matcher.py's substring-based `is_india_job()` and config's
`exclude_locations` city checks both see real, human-readable city text.

Live-verified 2026-09-06 totals: 15 of 376 global postings are India —
currently entirely Sales/GTM/Solution-Engineering/Finance/Marketing roles
for the India GCC (e.g. "Senior Solution Engineer - GCC", "Enterprise
Account Executive", "Technical Architect AI/ML", "Senior Data Engineer").
Zero matches against the standard keyword list today (0/15) — a genuine
current fact, not a fetcher defect: Snowflake's India board right now
has no title containing "software engineer"/"AI engineer"/"python
developer"/etc. verbatim, even though 2 of the 15 ("Technical Architect
AI/ML", "Senior Data Engineer") are clearly technical roles that just
don't match the shared keyword list's exact phrasing.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_NAME = "snowflake"
_API_URL = f"https://api.ashbyhq.com/posting-api/job-board/{_BOARD_NAME}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Module-level cache: the full board is fetched once and reused for every
# keyword/page call (Ashby's public board API ignores query params).
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _normalize_location(loc_str: str, is_india: bool) -> str:
    """Ensure India postings carry a literal 'India' substring.

    "IN-Pune" -> "Pune, India"; "IN-Bangalore-MSO" -> "Bangalore MSO, India";
    "IN-Delhi-Remote" -> "Delhi Remote, India"; "Bengaluru, India" is left
    as-is (already contains "india"). Non-India locations are returned
    unchanged.
    """
    loc = (loc_str or "").strip()
    if not is_india or "india" in loc.lower():
        return loc
    cleaned = re.sub(r"^IN-", "", loc).replace("-", " ").strip()
    return f"{cleaned}, India" if cleaned else "India"


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Snowflake Ashby board once and cache it.

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
                _API_URL,
                headers=_HEADERS,
                params={"includeCompensation": "true"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Snowflake: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Snowflake cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Snowflake cache fill: no response — {last_exc}")

    raw_jobs = r.json().get("jobs", [])
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        country = (
            (j.get("address") or {}).get("postalAddress", {}).get("addressCountry")
            or ""
        ).strip()
        is_india = country.lower() == "india"
        loc = _normalize_location(j.get("location") or "", is_india)

        published_at = j.get("publishedAt") or ""
        posting_date = published_at[:10] if published_at else ""

        app_url = j.get("jobUrl") or j.get("applyUrl") or ""

        _desc_cache[job_id] = _strip_html(j.get("descriptionHtml") or "")

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache = collected
    print(f"[Snowflake] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Snowflake jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    Ashby's public board API returns the same full board regardless of
    query params. All jobs (India and non-India) are returned — India
    scoping is left to matcher.py's is_india_job() / config
    exclude_locations, aided by the location normalisation documented in
    the module docstring.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Snowflake job.

    Served entirely from the cache filled by _fill_cache() — Ashby's board
    response already includes the full HTML `descriptionHtml` field for
    every posting, so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").split("/")[-1]
    description = _desc_cache.get(job_id, "")

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
