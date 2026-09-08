"""Fetches Tredence (data/AI analytics consultancy) job listings via RippleHire.

Tredence's marketing careers page (www.tredence.com/careers/greatest-of-ai)
links out to a RippleHire-hosted candidate site
(tredence.ripplehire.com/candidate) for the actual job board — the
informally-guessed "career.tredence.com" subdomain does not resolve (DNS
NXDOMAIN as of 2026-09-08). Same underlying ATS/API family as
CitiusTech/LTIMindtree/Mphasis (RippleHire), but with two real differences
found live rather than assumed from those siblings:

  - POST /candidate/candidatejobsearch with a JSON-encoded
    `careerSiteUrlParams` form field (page, search, token, source,
    pagesize) returns jobVoList — same shape as CitiusTech. The `search`
    keyword genuinely narrows server-side (different keywords return
    different totals; an empty search returns 0), so normal per-keyword
    pagination works with no full-cache-and-filter needed. A bare "ai"
    query returns 0 (likely a short-token/stopword no-op on this tenant,
    same class of quirk as Cisco's bare "AI") — not a bug, just means that
    slice of the default keyword list finds nothing on its own; other
    keywords ("AI engineer", "generative ai engineer") narrow correctly.
  - GET /candidate/candidatejobdetail is content-negotiated: with no
    `Accept` header it defaults to **XML** (confirmed via a bare `curl`),
    but with `Accept: application/json` (sent by every request this module
    makes) it consistently returns JSON — same `jobVO` shape as CitiusTech.
    Worth flagging in case a future edit ever drops the explicit Accept
    header, since the JSON parse would then fail silently on XML instead.

India detection (search results carry a bare `locations`/`jobLocation`
string, no country field, and this tenant is NOT India-only — it mixes in
US "San Jose (TR)"/"San Jose (HE)" entity codes and a Toronto site):
  - `locations` (preferred; `jobLocation` is null on a large fraction of
    real postings, but `locations` is always populated) is checked against
    a known-India city token whitelist (Bangalore/Bengaluru, Gurgaon/
    Gurugram, Pune, Hyderabad, Mumbai, Noida, Chennai, Kolkata, Coimbatore,
    Kochi, Chandigarh, Trivandrum, Lucknow, Nagpur, Madurai, Indore) before
    appending ", India" — Pune and Kolkata are real, active Tredence
    locations observed live and must still be excluded downstream via
    config's `exclude_locations`. Unrecognized strings (San Jose, Toronto,
    bare "Remote") are left untouched, which safely fails matcher.py's
    `is_india_job()` rather than guessing.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests

_TOKEN = "rzuz0vttMaz0VxxVzDiY"
_BASE = "https://tredence.ripplehire.com/candidate"
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

# Known India city/region tokens observed live for this tenant, plus every
# city named in config's exclude_locations (Pune, Kolkata already confirmed
# live; the rest included defensively so they're excluded, not dropped as
# unrecognized, if they ever appear).
_INDIA_TOKENS = (
    "bangalore", "bengaluru", "gurgaon", "gurugram", "pune", "hyderabad",
    "mumbai", "noida", "chennai", "kolkata", "coimbatore", "kochi",
    "chandigarh", "kerala", "trivandrum", "lucknow", "nagpur", "madurai",
    "indore", "delhi", "india",
)
# Known non-India signals observed live for this tenant.
_NON_INDIA_TOKENS = ("san jose", "toronto", "canada", "usa", "(tr)", "(he)")


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]*>?", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-MMM-YYYY' (e.g. '20-Aug-2026') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_location(job: dict) -> str:
    """Best-effort India detection/normalization for a RippleHire job row."""
    raw = (job.get("locations") or job.get("jobLocation") or "").strip()
    if not raw:
        return ""

    loc_lower = raw.lower()
    if any(tok in loc_lower for tok in _NON_INDIA_TOKENS):
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
    """Return a page of Tredence jobs matching *keyword*.

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
                raise RateLimitError("Tredence: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Tredence fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Tredence fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("Tredence: non-JSON response (missing Referer?)")

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
    """Return (description, posting_date) for a single Tredence job."""
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
                raise RateLimitError("Tredence description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Tredence description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Tredence description fetch: no response — {last_exc}")

    try:
        job_vo = r.json().get("jobVO", {}) or {}
    except ValueError:
        raise RateLimitError("Tredence description: non-JSON response (missing Accept header?)")

    parts = [_strip_html(job_vo.get("jobSkills", "")), _strip_html(job_vo.get("jobDesc", ""))]
    description = " ".join(p for p in parts if p)
    posting_date = _parse_date(job_vo.get("jobPostingDate", "") or "")
    return description, posting_date
