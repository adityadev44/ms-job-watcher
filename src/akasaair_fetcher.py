r"""Fetches Akasa Air job listings — Zoho Recruit ATS.

ATS discovery (live, 2026-09-07): akasaair.com/careers-at-akasa-air links out
to `akasaair.zohorecruit.in/jobs/Careers` — the same Zoho Recruit
"career-website" product already used by yubi_fetcher.py in this repo, a
different tenant/portal. The public careers listing page is fully
server-rendered (confirmed via a live Playwright network capture: no
separate XHR/API call for the job list at all, just one HTML response) —
the entire board is embedded as an HTML-entity-encoded JSON array inside one
hidden input:

    GET https://akasaair.zohorecruit.in/jobs/Careers
        -> <input type="hidden" value="[{&#34;Client_Name&#34;:...}]" id="jobs">

A plain `requests.get()` with a browser User-Agent returns the identical
HTML — no Playwright needed, same as Yubi's tenant.

**Gotcha (different from yubi_fetcher.py's marker approach): don't anchor on
a literal leading `[{&#34;Company&#34;` substring.** This tenant's first
JSON key is `Client_Name`, not `Company` (Zoho Recruit lets each tenant
customize which field renders first), and critically the page ALSO contains
*earlier*, unrelated `<input type="hidden" value="[{...">` elements before
the real `id="jobs"` one (a `moduleMeta` input, etc.) with no distinguishing
prefix of their own. Anchoring on a field-name substring or naively
searching forward from the first `<input ... value="[{` breaks non-greedy
regex matching across those unrelated earlier inputs. The robust approach
used here: find the literal `" id="jobs">` suffix first (only one instance
of the id on this page), then walk backward with `rfind('value="', ...)` to
find the START of THAT SPECIFIC input's value — anchoring from the known
suffix rather than an assumed-unique prefix.

Confirmed live: exactly 20 open postings (matches the page's own "Full time
(20)" job-type facet count) — this appears to be the whole board, not a
truncated first page: `?page=2` was tried and silently returns the
identical 20 jobs/same IDs (the param is ignored), so no pagination is
implemented — the whole (small) board is cached once per process, same
policy as Yubi/CRED/Prefr elsewhere in this repo.

Every job on this tenant already carries `Job_Description` (full HTML,
confirmed non-trivial length, e.g. 2200+ chars on a real posting) INLINE on
the list page — unlike Yubi's tenant, which required a separate per-job
detail-page fetch with a gnarly two-layer JS-string-escape decode.
`fetch_job_description` here is served entirely from the list-page cache,
no extra request needed (same "already inline" shape as
darwinbox_fetcher.py/perfios_fetcher.py).

Location: every current posting is genuinely India (`Country` is always
literally `"India"`). `City` is inconsistent in casing/format across
postings (`"Mumbai"`, `"Gurugram"`, `"Pan-India"`, `"PAN India"`, even the
literal string `"NA"` for one posting) — reused the same
`city`/`country`-substring-dedup logic as yubi_fetcher.py's
`_location_from_job` rather than hand-listing every casing variant.

`Date_Opened` is already `YYYY-MM-DD` — used as-is for `posting_date`, no
parsing needed. (Several open postings carry a 2023 `Date_Opened` despite
being live today in 2026 — a genuine data-quality quirk of this tenant's
Zoho config, not a fetcher bug; the field is used as-is per the repo's
"don't guess/correct upstream data" convention.)

Application URL: `https://akasaair.zohorecruit.in/jobs/Careers/{id}` —
confirmed live that the bare numeric-ID URL (no slug segment at all)
resolves to the correct job's detail page.
"""
from __future__ import annotations

import re
import time

import requests

_LIST_URL = "https://akasaair.zohorecruit.in/jobs/Careers"
_JOB_PAGE_BASE = "https://akasaair.zohorecruit.in/jobs/Careers/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_JOBS_INPUT_SUFFIX = '" id="jobs">'
_VALUE_MARKER = 'value="'

# Module-level cache: the list endpoint has no working query params and
# always returns the full (small) board, so fetch once per process.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _location_from_job(j: dict) -> str:
    city = (j.get("City") or "").strip()
    country = (j.get("Country") or "").strip()
    if city and country:
        if country.lower() in city.lower():
            return city
        return f"{city}, {country}"
    return city or country or "India"


def _get_html(url: str, *, timeout: int = 20, context: str = "") -> str:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Akasa Air {context}: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Akasa Air {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Akasa Air {context}: no response — {last_exc}")


def _extract_jobs_blob(html_text: str) -> str | None:
    """Find the specific ``id="jobs"`` hidden input's value, anchoring from
    the (unique) ``" id="jobs">`` suffix rather than an assumed-unique
    value-prefix — see module docstring for why the naive prefix approach
    breaks on this tenant."""
    import html as html_mod
    end = html_text.find(_JOBS_INPUT_SUFFIX)
    if end == -1:
        return None
    start = html_text.rfind(_VALUE_MARKER, 0, end)
    if start == -1:
        return None
    start += len(_VALUE_MARKER)
    return html_mod.unescape(html_text[start:end])


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Akasa Air (Zoho Recruit) board once and cache it.

    ``_cache_filled``/``_cache_error`` are set before/during the one real
    fetch so a failure never silently becomes an empty-but-"successful"
    cache on a later call in the same process (Honeywell/Persistent
    lesson — see airindia_fetcher.py).
    """
    global _cache_filled, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    import json

    try:
        html_text = _get_html(_LIST_URL, timeout=timeout, context="job list")
        blob = _extract_jobs_blob(html_text)
        if blob is None:
            raise RateLimitError("Akasa Air: jobs hidden-input not found in list page")
        raw_jobs = json.loads(blob)
    except RateLimitError as exc:
        _cache_error = exc
        raise
    except ValueError as exc:
        _cache_error = RateLimitError(f"Akasa Air job list: invalid JSON — {exc}")
        raise _cache_error from exc

    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "").strip()
        title = (j.get("Posting_Title") or j.get("Job_Opening_Name") or "").strip()
        if not (job_id and title):
            continue
        collected.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(j),
            "posting_date": (j.get("Date_Opened") or "").strip(),
            "application_url": f"{_JOB_PAGE_BASE}{job_id}",
        })
        _desc_cache[job_id] = _strip_html(j.get("Job_Description") or "")

    _job_cache[:] = collected
    print(f"[Akasa Air] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Akasa Air (Zoho Recruit) jobs from the cached board.

    ``keyword``/``location`` are accepted for interface compatibility but
    ignored: the list page has no working query params (`?page=2` silently
    returns page 1 again — confirmed live) and always returns the full
    ~20-job board; the shared matcher does the real title/skill/India
    filtering.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Akasa Air job.

    Served entirely from the cache filled by ``fetch_jobs`` — the list page
    already returns each job's full ``Job_Description`` HTML inline, so no
    separate per-job detail request is needed.
    """
    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
