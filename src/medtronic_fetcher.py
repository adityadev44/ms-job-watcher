"""Fetches Medtronic job listings via the Workday public REST API.

Medtronic's public careers domain (jobs.medtronic.com) redirects straight
to the Workday tenant (medtronic.wd1.myworkdayjobs.com/medtroniccareers) --
confirmed live 2026-09-13, no intermediate frontend skin (unlike Boeing/
GE Aerospace/GE HealthCare's TalentBrew/Phenom skins over a Workday
backend). A direct POST to
/wday/cxs/medtronic/medtroniccareers/jobs returns real data (HTTP 200,
total=1139 globally).

Unlike most Workday tenants in this repo, this one exposes no flat
country facet at all (only `jobFamilyGroup`/`remoteType`/`timeType`/
`workerSubType`/`locationMainGroup`, and `locationMainGroup`'s nested
values are individual city-level entries, not a usable country rollup --
same "no clean India facet" shape as Fiserv/Genpact/FactSet). However,
`locationsText` reliably includes the literal substring "India" for every
India posting observed (e.g. "Nanakramguda, Hyderabad, India") -- no
client-side append needed, unlike Novartis/MSD -- so this fetcher fetches
globally per keyword and filters India client-side via `is_india_job()`
in matcher.py, the same pattern as Fiserv/Genpact/FactSet.

Confirmed live: `searchText` is a genuine server-side full-text filter on
this tenant (searchText="" -> 1139 total, "software engineer" -> 241,
"python" -> 81, ".net" -> 226) -- NOT ignored, so this fetcher is not
added to `_IGNORES_KEYWORDS`.

Confirmed live 2026-09-13: Medtronic operates a real, active Hyderabad
(Nanakramguda) software-engineering GCC, not just pharma/medtech R&D or
field sales -- current matching postings include "Director, Software
Engineering & AI", "Senior Principal Software Engineer", "Lead Enterprise
Software Engineer", and "Senior Principal Enterprise Software Engineer"
(this last one's JD explicitly names both C# and Python -- a genuine
`.NET / C#`-track primary-skill match, not just a broad-only hit). DO NOT
add `require_tech_in_description` -- titles here are genuine engineering
titles (not generic IT-services level-bands), Layer 3 alone is the right
precision level, same reasoning as Novartis/MSD/GE HealthCare.

Two Workday quirks confirmed live on this tenant, both guarded here:
- Page size is capped at 20 -- limit=25/50 both return a raw HTTP 400
  (same class as Northern Trust/Pfizer/Novartis/MSD/Abbott).
- Pagination wraps around past the true total for a keyword (same bug
  class as UBS/MUFG/Nvidia/Pfizer/Walmart/Novartis/Abbott): offset past
  the real last page replays page 1's exact job set verbatim instead of
  returning an empty list. Guarded via the same page1-first-ID memo
  pattern as those fetchers (tracked per-keyword, since results differ
  per keyword on this tenant).

Job detail descriptions use the same Workday CXS JSON detail API shape as
Novartis/Pfizer/MSD/Abbott: GET
.../wday/cxs/medtronic/medtroniccareers{externalPath} returns a
`jobPostingInfo.jobDescription` HTML blob and an already-ISO
`jobPostingInfo.startDate`.
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://medtronic.wd1.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/medtronic/medtroniccareers/jobs"
_JOB_BASE = f"{_BASE_URL}/medtroniccareers"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/medtronic/medtroniccareers"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS/MUFG/Nvidia/Pfizer/Walmart/Novartis/Abbott): requesting
# offset >= total does NOT return an empty jobPostings array -- it silently
# re-returns page 1 verbatim once past the genuine last page. Track each
# keyword's first-page first job ID and treat a repeat of it on a later
# page as "no more results".
_page1_first_id: dict[str, str] = {}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/medtroniccareers",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


def _parse_posted_on(posted_on: str) -> str:
    """Convert Workday's relative date string to YYYY-MM-DD."""
    if not posted_on:
        return ""
    s = posted_on.strip().lower()
    today = date.today()

    if "today" in s:
        return today.strftime("%Y-%m-%d")
    if "yesterday" in s:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if "30+" in s:
        return (today - timedelta(days=30)).strftime("%Y-%m-%d")

    m = re.search(r"(\d+)\s+day", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+week", s)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+month", s)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")

    return ""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    body = {
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    json=body,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Medtronic Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Medtronic fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

    # Detect the pagination wrap-around bug (see _page1_first_id comment
    # above) before doing any other work on this page.
    if postings:
        first_bullets = postings[0].get("bulletFields", [])
        first_id = first_bullets[0].strip() if first_bullets else ""
        if start == 0:
            if first_id:
                _page1_first_id[keyword] = first_id
        elif first_id and _page1_first_id.get(keyword) == first_id:
            return []

    jobs: list[dict] = []
    for p in postings:
        loc = p.get("locationsText", "").strip()
        # No country facet on this tenant -- fetch globally and filter India
        # here via a word-boundary regex, same as PayPal/FactSet/DAZN. A
        # plain "india" substring check is NOT safe: matcher.py's own
        # is_india_job() does only a plain substring check, and this tenant
        # has real US postings in Warsaw, INDIANA (confirmed live: job
        # R76802, "Engineering Tech V - CNC Programming", would otherwise
        # leak through as a false India match on "Indiana").
        if not loc or not re.search(r"\bindia\b", loc, re.IGNORECASE):
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        external_path = p.get("externalPath", "")

        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets and bullets[0].strip() else external_path
        if not job_id:
            continue

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date). startDate in the detail
    response is already ISO (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/medtroniccareers/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    api_url,
                    headers=_HEADERS,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                raise RateLimitError("Medtronic description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Medtronic description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
