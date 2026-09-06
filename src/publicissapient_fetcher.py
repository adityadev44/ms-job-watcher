"""
Publicis Sapient job fetcher — iCIMS Jibe REST API via careers.publicisgroupe.com.

ATS discovery (2026-09-05): careers.publicissapient.com redirects to
careers.publicisgroupe.com (Publicis Groupe's parent career portal). The job
search on careers.publicissapient.com/job-search uses the iCIMS ATS backend
confirmed via the referral URL `referral-publicisgroupe.icims.com`. The
public job search REST endpoint is at:

    GET https://careers.publicisgroupe.com/api/jobs
    ?keywords=<kw>&location=India&limit=<n>&offset=<start>

This is the same iCIMS Jibe REST API family used by Gallagher (jobs.ajg.com),
Schneider Electric (careers.se.com), S&P Global Careers, etc. in this repo.
Unlike those tenants, `careers.publicisgroupe.com` does not use a branded
Jibe domain — it's iCIMS' underlying REST endpoint behind Publicis Groupe's
custom Angular SPA.

Verified live (2026-09-05):
- ~200 total Publicis Groupe India postings (location=India filter works
  server-side), but only ~60 are Publicis Sapient brand after brand filtering.
  The remaining ~140 belong to Epsilon, Digitas, MSL, Publicis Media, and
  other Publicis Groupe sub-brands sharing the same portal.
- `keywords` param is a genuine server-side filter: 0 results for a nonsense
  token; 32 results for ".net developer", 43 for "software engineer".
  However, keywords were found to amplify non-Sapient brand contamination
  (Epsilon/Digitas results dominate keyword searches), so keywords are
  deliberately NOT passed — the brand filter handles Sapient isolation.
- `offset` paginates cleanly (no wraparound observed).
- Full job description, qualifications, posted_date, and apply_url are all
  inline in the search response — no per-job detail fetch needed.
- apply_url points at `sapient-publicisgroupe.icims.com/jobs/<req_id>/login`
  (the iCIMS application page for Publicis Sapient's tenant).

Quirks:
- `full_location` field lists all locations for multi-site postings (e.g.
  "Bengaluru, India; Gurgaon, India; Hyderabad, India"). `short_location`
  only shows the primary site. `full_location` is preferred for matcher.py's
  is_india_job() substring check to avoid missing multi-site India postings.
- `posted_date` is ISO-8601 with timezone offset (e.g.
  "2026-04-13T12:44:00+0000") — sliced to 10 chars for YYYY-MM-DD.
- The `description` and `qualifications` fields are already plain text (no
  HTML markup observed across the live sample) — `_strip_html` is kept as
  defense in depth.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent network failure."""

_BASE_URL = "https://careers.publicisgroupe.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/jobs",
}

_MAX_PAGE_SIZE = 100  # observed limit; stay at/under this defensively

# Description cache: application_url -> (description, posting_date)
# Populated during fetch_jobs() since descriptions are inline.
_desc_cache: dict[str, tuple[str, str]] = {}

# Pagination-wraparound guard
_FIRST_PAGE_IDS: set[str] | None = None


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-04-13T12:44:00+0000' -> '2026-04-13'."""
    return raw[:10] if raw else ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return one page of Publicis Sapient India job listings.

    Both `keywords` and `location=India` are genuine server-side filters.
    `offset` paginates cleanly. Descriptions are inline and cached during
    this call so fetch_job_description() never makes a network request.
    """
    global _FIRST_PAGE_IDS

    # Keywords are deliberately NOT passed to the API. The Publicis Groupe
    # API serves all brands; keyword-filtered results are overwhelmingly
    # non-Sapient (mostly Epsilon). The brand filter applied below isolates
    # the ~60 Publicis Sapient India jobs from the ~200 total Publicis
    # Groupe India pool. All title/skill matching is done by matcher.py.
    params = {
        "location": location or "India",
        "limit": min(num, _MAX_PAGE_SIZE),
        "offset": start,
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(
                    f"Publicis Sapient: 429 rate-limited on attempt {attempt + 1}"
                )
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(
                    f"Publicis Sapient search failed after 3 attempts: {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    try:
        raw_jobs = (r.json() if r else {}).get("jobs", [])
    except ValueError as exc:
        raise RateLimitError(
            f"Publicis Sapient search returned non-JSON body: {exc}"
        ) from exc

    jobs: list[dict] = []
    for item in raw_jobs:
        j = item.get("data", {})
        job_id = str(j.get("req_id") or j.get("slug") or "")
        if not job_id:
            continue

        title = (j.get("title") or "").strip()
        if not title:
            continue

        # Filter to Publicis Sapient brand only. The Publicis Groupe API serves
        # all brands; apply_url tenant or tags2 identifies the brand.
        # sapient-publicisgroupe.icims.com = Publicis Sapient India jobs.
        # careers-publicisgroupe.icims.com and epsilon-publicisgroupe.icims.com
        # are other Publicis brands that appear when keyword searching.
        apply_url = (j.get("apply_url") or "").strip()
        tags2 = j.get("tags2") or []
        is_sapient = (
            "sapient-publicisgroupe" in apply_url.lower()
            or any("sapient" in str(t).lower() for t in tags2)
        )
        if not is_sapient:
            continue

        if not apply_url:
            apply_url = f"https://sapient-publicisgroupe.icims.com/jobs/{job_id}/login"

        # Prefer full_location for multi-site postings; fall back to short_location
        loc = (j.get("full_location") or j.get("short_location") or "").strip()
        posting_date = _parse_date(j.get("posted_date") or j.get("create_date") or "")

        # Build inline description from description + qualifications
        raw_desc = " ".join(
            part
            for part in (
                j.get("description", ""),
                j.get("qualifications", ""),
            )
            if part
        )
        description = _strip_html(raw_desc)

        _desc_cache[apply_url] = (description, posting_date)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": apply_url,
        })

    # Pagination-wraparound guard
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) — served from the cache fetch_jobs() built.

    The Publicis Groupe search API includes full descriptions inline, so no
    separate HTTP call is ever needed.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]
    return "", ""
