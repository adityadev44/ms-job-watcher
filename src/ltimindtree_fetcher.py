"""Fetches LTIMindtree (rebranded "LTM") India job listings via RippleHire.

LTIMindtree's public careers.ltimindtree.com search only lists ~80 overseas
lateral roles (zero India results) — their actual India hiring runs through
a separate RippleHire-hosted career site at ltimindtree.ripplehire.com,
linked from https://www.ltm.com/india-careers ("Opportunities in India").

RippleHire is a JSON API (no Playwright needed):
  - POST /candidate/candidatejobsearch with a JSON-encoded
    `careerSiteUrlParams` form field (page, search, token, source, pagesize,
    geo) returns jobVoList. Unlike most in-house ATSes here, the `search`
    keyword IS applied server-side, so normal per-keyword pagination works
    (no full-cache-and-filter needed).
  - Search results carry no posting date (openDate/jobPostingDate all null);
    GET /candidate/candidatejobdetail?jobSeq={id} has the real
    jobPostingDate ("DD-MMM-YYYY") plus jobSkills (a short mandatory-skills
    line) alongside jobDesc — both are concatenated into the description
    text since jobSkills often names the stack before jobDesc does.
  - Requires a Referer header matching the candidate site or the API
    returns an empty body instead of JSON.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime

import requests

_TOKEN = "xviyQvbnyYZdGtozXoNm"
_BASE = "https://ltimindtree.ripplehire.com/candidate"
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


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-MMM-YYYY' (e.g. '01-Jul-2026') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of LTIMindtree India jobs matching *keyword*.

    RippleHire paginates by 0-indexed `page` + `pagesize`, not start/num
    offsets, so page is derived assuming a constant page size across calls.
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
        "geo": "India",
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
                raise RateLimitError("LTIMindtree: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"LTIMindtree fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"LTIMindtree fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("LTIMindtree: non-JSON response (missing Referer?)")

    jobs: list[dict] = []
    for job in data.get("jobVoList", []) or []:
        job_id = job.get("jobSeq") or job.get("jobId")
        title = (job.get("jobTitle") or "").strip()
        if not (job_id and title):
            continue
        city = (job.get("locations") or "").strip()
        location_str = f"{city}, India" if city else "India"
        jobs.append({
            "id": str(job_id),
            "title": title,
            "location": location_str,
            "posting_date": "",  # not present in search results; filled on detail fetch
            "application_url": f"{_CANDIDATE_PAGE}#detail/job/{job_id}",
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single LTIMindtree job."""
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
                raise RateLimitError("LTIMindtree description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"LTIMindtree description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"LTIMindtree description fetch: no response — {last_exc}")

    try:
        job_vo = r.json().get("jobVO", {})
    except ValueError:
        raise RateLimitError("LTIMindtree description: non-JSON response")

    parts = [_strip_html(job_vo.get("jobSkills", "")), _strip_html(job_vo.get("jobDesc", ""))]
    description = " ".join(p for p in parts if p)
    posting_date = _parse_date(job_vo.get("jobPostingDate", "") or "")
    return description, posting_date
