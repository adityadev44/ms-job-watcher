"""Fetches CitiusTech (healthcare IT services) India job listings via RippleHire.

CitiusTech's marketing careers page (www.citiustech.com/careers) links out to
a RippleHire-hosted candidate site (citiustech.ripplehire.com) for the actual
job board — the informally-referenced "careers.citiustech.com" subdomain does
not resolve (DNS NXDOMAIN as of 2026-08-31); the real portal lives entirely
on the RippleHire tenant. Same underlying ATS/API shape as LTIMindtree
(ltimindtree_fetcher.py):

  - POST /candidate/candidatejobsearch with a JSON-encoded
    `careerSiteUrlParams` form field (page, search, token, source, pagesize)
    returns jobVoList. The `search` keyword IS applied server-side (an empty
    search returns 0 jobs; different keywords return different result sets),
    so normal per-keyword pagination works — no full-cache-and-filter needed.
  - Unlike LTIMindtree, this tenant is NOT India-only — it mixes India roles
    with US roles (Denver CO, Portland OR, Dallas TX, Princeton NJ, etc.) and
    a `geo` search param that looks like it should restrict by country
    (`geo=India`/`geo=IN`) instead silently zeroes every result, so it must
    be omitted entirely and India detection done client-side.
  - Search results carry no posting date and an empty jobDesc; a separate
    GET /candidate/candidatejobdetail?jobSeq={id} has the real
    jobPostingDate ("DD-MMM-YYYY") plus jobDesc — concatenated with any
    jobSkills text (usually empty here, but LTIMindtree's tenant uses it, so
    kept for parity) for the description.
  - Requires a Referer header matching the candidate site or the API
    returns an empty body instead of JSON.

India detection (no location facet, no reliable country param):
  - Most jobs carry only a `locations` facility-name string (e.g.
    "CitiusTech Hyderabad", "CT Pune Qubix SEZ1", "CitiusTech Headoffice")
    with no "India" substring anywhere — client-side normalization is
    required or every India job would be silently dropped by matcher.py's
    `is_india_job()`.
  - A minority of jobs carry a separate `jobLocation` field that is
    literally the string "India" when set — used as a first, authoritative
    signal when present.
  - Otherwise, `locations` text is checked against known CitiusTech India
    facility/city tokens (citiustech, ct pune, mumbai, pune, bengaluru,
    bangalore, chennai, hyderabad, gurgaon, gurugram, noida) vs known
    non-India tokens (US city names observed in this tenant's data, "(USA)",
    "-US" suffix, or a bare "Remote" with no city at all) before deciding
    whether to append ", India". Ambiguous/unrecognized location strings are
    left untouched, which fails `is_india_job()` safely (excluded, not
    mislabeled) rather than guessing.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests

_TOKEN = "bCKlfz3OO8vQIgiM2vuI"
_BASE = "https://citiustech.ripplehire.com/candidate"
_SEARCH_URL = f"{_BASE}/candidatejobsearch"
_DETAIL_URL = f"{_BASE}/candidatejobdetail"
_CANDIDATE_PAGE = f"{_BASE}/?token={_TOKEN}&lang=en&source=CAREERSITE"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": _CANDIDATE_PAGE,
}

# Known non-India signals observed in this tenant's raw `locations` text
# (North American offices + a Philippines-flagged remote job code prefix).
_NON_INDIA_TOKENS = (
    "(usa)", "-us", "denver", "portland", "dallas", "brentwood",
    "jacksonville", "princeton",
)
# Known India facility/city tokens for this tenant.
_INDIA_TOKENS = (
    "citiustech", "ct pune", "mumbai", "pune", "bengaluru", "bangalore",
    "chennai", "hyderabad", "gurgaon", "gurugram", "noida",
)


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-MMM-YYYY' (e.g. '15-Jun-2026') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_location(job: dict) -> str:
    """Best-effort India detection/normalization for a RippleHire job row.

    See module docstring — this tenant has no reliable server-side country
    filter, so ambiguous or unrecognized strings are returned unchanged
    (which safely fails matcher.py's `is_india_job()`) rather than guessed.
    """
    raw = (job.get("locations") or "").strip()
    if not raw:
        return ""

    job_location_field = job.get("jobLocation")
    if isinstance(job_location_field, str) and job_location_field.strip().lower() == "india":
        return raw if "india" in raw.lower() else f"{raw}, India"

    loc_lower = raw.lower()
    if any(tok in loc_lower for tok in _NON_INDIA_TOKENS) or loc_lower == "remote":
        return raw

    if any(tok in loc_lower for tok in _INDIA_TOKENS):
        return raw if "india" in loc_lower else f"{raw}, India"

    return raw


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of CitiusTech jobs matching *keyword*.

    RippleHire paginates by 0-indexed `page` + `pagesize`, not start/num
    offsets, so page is derived assuming a constant page size across calls.
    India filtering happens later in matcher.py via `is_india_job()`; this
    function only normalizes location text (see `_normalize_location`).
    """
    if not keyword:
        return []

    page_num = start // num if num else 0
    params = {
        "page": page_num,
        "search": keyword,
        "token": _TOKEN,
        "source": "CAREERSITE",
        "pagesize": num,
    }

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(
                _SEARCH_URL,
                data={"careerSiteUrlParams": json.dumps(params), "lang": "en"},
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("CitiusTech: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"CitiusTech fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"CitiusTech fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("CitiusTech: non-JSON response (missing Referer?)")

    jobs: list[dict] = []
    for job in data.get("jobVoList", []) or []:
        job_id = job.get("jobSeq") or job.get("jobId")
        title = (job.get("jobTitle") or "").strip()
        if not (job_id and title):
            continue
        jobs.append({
            "id": str(job_id),
            "title": title,
            "location": _normalize_location(job),
            "posting_date": "",  # not present in search results; filled on detail fetch
            "application_url": f"{_CANDIDATE_PAGE}#detail/job/{job_id}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single CitiusTech job."""
    m = re.search(r"#detail/job/(\d+)", application_url)
    job_seq = m.group(1) if m else ""

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _DETAIL_URL,
                params={
                    "token": _TOKEN,
                    "jobSeq": job_seq,
                    "source": "CAREERSITE",
                    "lang": "en",
                },
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("CitiusTech description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"CitiusTech description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"CitiusTech description fetch: no response — {last_exc}")

    try:
        job_vo = r.json().get("jobVO", {})
    except ValueError:
        raise RateLimitError("CitiusTech description: non-JSON response")

    parts = [_strip_html(job_vo.get("jobSkills", "")), _strip_html(job_vo.get("jobDesc", ""))]
    description = " ".join(p for p in parts if p)
    posting_date = _parse_date(job_vo.get("jobPostingDate", "") or "")
    return description, posting_date
