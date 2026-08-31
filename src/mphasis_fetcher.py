"""Fetches Mphasis India job listings via RippleHire.

Mphasis's careers site (careers.mphasis.com) is a RippleHire-hosted career
portal (mphasis.ripplehire.com, token `ty4DfyWddnOrtpclQeia`) -- the same
ATS this repo already integrates for LTIMindtree. Confirmed live 2026-08-31:

  - POST /candidate/candidatejobsearch with a JSON-encoded
    `careerSiteUrlParams` form field (page, search, token, source, pagesize)
    returns jobVoList, and the `search` keyword IS applied server-side
    (counts genuinely differ per keyword: "engineer" -> 82, ".net" -> 8,
    "langchain" -> 5), so normal per-keyword pagination works like
    LTIMindtree -- no full-cache-and-filter needed.
  - UNLIKE LTIMindtree, a `geo` (or `location`) param is NOT usable here --
    adding `"geo": "India"` (or "india") to the request silently zeroes
    `totalJobCount` to 0 even though India jobs exist in the unfiltered
    pool. Search results carry no country field at all (`locations` is a
    bare city name, e.g. "Bangalore"/"Chennai"/"Hyderabad"/"Mumbai"/"Pune"
    /"Noida", verified via the response's own `cityCount` facet) -- so this
    fetcher fetches globally per keyword and recognises India via a city
    whitelist, appending ", India" only for known India cities (same
    split-the-difference pattern as `lowes_fetcher.py`), leaving everything
    else untouched so matcher.py's `is_india_job()` rejects it.
  - Search results carry no posting date (openDate/jobPostingDate null);
    GET /candidate/candidatejobdetail?jobSeq={id} has the real
    jobPostingDate ("DD-MMM-YYYY") plus jobSkills (a short mandatory-skills
    line, e.g. "PRIMARY SKILL : .Net Framework ... SECONDARY SKILL : C#")
    alongside jobDesc -- both concatenated into the description text, same
    as LTIMindtree.
  - Unlike LTIMindtree, a Referer header is NOT required here (verified:
    omitting it still returns real JSON) -- kept anyway to match the
    browser-like request shape and because RippleHire tenants have been
    inconsistent about this before.
  - Titles are heavily generic/level-banded IT-services titles ("Senior
    Software Engineer", "Module Lead", "Delivery Module Lead", "Technical
    Architect") that hide wildly different actual stacks -- live-checked
    "Senior Software Engineer" postings turned out to be Salesforce LWC,
    Selenium/UFT test automation, and Java/AWS roles with zero .NET/AI
    content. A genuine .NET/C# role (job 707847, "Senior Software Engg -
    Systems", Bangalore) and real LangChain-adjacent AI postings do exist
    in the pool, but title text alone cannot separate them from the noise
    -- `require_tech_in_description` is enabled for this company.
"""
from __future__ import annotations

import html as html_mod
import re
import json
import time
from datetime import datetime

import requests

_TOKEN = "ty4DfyWddnOrtpclQeia"
_BASE = "https://mphasis.ripplehire.com/candidate"
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

# India city tokens seen on this tenant (search results carry no country
# field -- `locations` is a bare city name). Append ", India" only for these
# recognised cities so matcher.py's is_india_job() can see it; everything
# else (Georgia, Texas, Tokyo, Singapore, Toronto, ...) passes through
# unmodified and is correctly rejected as non-India.
_INDIA_CITIES = (
    "bangalore", "bengaluru", "chennai", "hyderabad", "mumbai", "pune",
    "noida", "gurugram", "gurgaon", "delhi", "kolkata", "chandigarh",
    "kochi", "trivandrum", "indore", "nagpur", "lucknow", "madurai",
)


def _is_india_city(loc: str) -> bool:
    low = loc.lower()
    return any(city in low for city in _INDIA_CITIES)


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
    """Return a page of Mphasis job listings matching *keyword*.

    No `geo` param is sent (see module docstring -- it zeroes results on
    this tenant); India detection happens client-side via a city whitelist.
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
                raise RateLimitError("Mphasis: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Mphasis fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Mphasis fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("Mphasis: non-JSON response")

    jobs: list[dict] = []
    for job in data.get("jobVoList", []) or []:
        job_id = job.get("jobSeq") or job.get("jobId")
        title = (job.get("jobTitle") or "").strip()
        if not (job_id and title):
            continue
        city = (job.get("locations") or "").strip()
        location_str = f"{city}, India" if city and _is_india_city(city) else city
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
    """Return (description, posting_date) for a single Mphasis job."""
    m = re.search(r"#detail/job/(\S+)", application_url)
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
                raise RateLimitError("Mphasis description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Mphasis description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Mphasis description fetch: no response — {last_exc}")

    try:
        job_vo = r.json().get("jobVO", {})
    except ValueError:
        raise RateLimitError("Mphasis description: non-JSON response")

    parts = [_strip_html(job_vo.get("jobSkills", "")), _strip_html(job_vo.get("jobDesc", ""))]
    description = " ".join(p for p in parts if p)
    posting_date = _parse_date(job_vo.get("jobPostingDate", "") or "")
    return description, posting_date
