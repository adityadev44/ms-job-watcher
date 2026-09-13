"""Fetches Bajaj Finance Limited's job listings via its PeopleStrong
"Candidate Portal" ATS (bflcareers.peoplestrong.com) -- the same Indian
HR-tech vendor already onboarded for Larsen & Toubro
(``larsentoubro_fetcher.py``) and Aditya Birla Group
(``adityabirla_fetcher.py``), a different tenant here.

Entity chosen (live investigation, 2026-09-13): the Bajaj group spans
several separately listed/managed entities (Bajaj Auto, Bajaj Finserv Ltd,
Bajaj Finance Ltd, Bajaj Allianz, Bajaj Markets, Bajaj Finserv Health).
Bajaj Finserv's own branded "Careers" page
(bajajfinserv.in/careers-finance) links to a Zoho Recruit tenant
(bajajfinserv.zohorecruit.in/jobs/Careers, `company_name: "Bajaj Finance"`)
that turned out to have exactly ONE stale posting from 2023 -- confirmed
live by parsing its own embedded job-list JSON, not a request-shape bug.
That same bajajfinserv.in careers page also links out to
``https://bflcareers.peoplestrong.com/home`` (a 404, but the site's SPA
root at ``bflcareers.peoplestrong.com/`` is live) -- this is Bajaj
Finance's REAL, actively used India hiring pipeline: a PeopleStrong
Angular SPA with a genuine, large (9,220 total) job pool including regular
current SDE-track postings (Bengaluru "Senior Software Engineer"/"Senior
Data Engineer" roles were live and verified at investigation time,
alongside a much larger Pune-heavy pool -- Pune is this repo's own
`exclude_locations` entry, so those don't reach the alert feed, which is
expected/correct, not a bug). Chosen over Bajaj Auto (a traditional
two/three-wheeler manufacturer with no comparable software-engineering
hiring surface found) and Bajaj Allianz/Markets (not independently
investigated once Bajaj Finance's own live, working, large SDE-relevant
pipeline was confirmed).

Same REST API shape as L&T/Aditya Birla, confirmed live and reproducible
via plain ``requests`` with no auth/session/cookies:

  - Search: ``POST /api/cp/rest/altone/cp/jobs/v1?offset=<n>&limit=<n>
    &searchString=<kw>`` with the same fixed, mostly-null JSON body
    (`{"bandList": null, "gradeList": null, "bandIDList": [],
    "gradeIDList": [], "employeeCategoryLabelList": null}`) the real page
    sends. Response: ``{"totalRecords": <int>, "response": [{
    requisitionId, jobTitle, jobPostedDate ("YYYY-MM-DD HH:MM:SS.f"),
    locationHierarchyComplete ("India>MAHARASHTRA>West>Pune>Pune Corporate
    Office - Mantri>Megapolis"), jobDetailUrl, ...}]}``.
  - ``searchString`` genuinely narrows server-side (confirmed live
    2026-09-13, literal case-insensitive substring on the title, same
    non-tokenized behavior as L&T's tenant): unfiltered = 9220;
    "software engineer" = 74; "senior software engineer" = 64; "data
    engineer" = 18; "AI engineer" = 2; "developer" = 7; "java" = 0;
    "python" = 0; ".NET developer"/"C# developer"/"dot net"/"angular"/
    "machine learning engineer"/"generative ai engineer" = 0 right now
    (genuinely zero current title matches, not a broken filter -- "engineer"
    alone = 105 and "" = 9220, so the search is clearly alive and narrowing).
  - Pagination: ``offset``/``limit`` map directly onto ``start``/``num``.
    Confirmed live an out-of-range offset against a real (105-job) pool
    returns an empty ``response`` list rather than wrapping to page 1 --
    same safe-termination behavior as L&T's tenant. ``limit`` silently caps
    at 99 rows per request regardless of a larger requested value
    (irrelevant in practice: matcher.py's own page size is 20).
  - Location: the ``locationHierarchyComplete`` string's leading segment is
    always the real country ("India" for domestic postings) -- confirmed
    reliable, same hand-list-free signal as L&T's tenant.

Detail page: the SPA's own ``jobDetailUrl`` (e.g. ".../job/detail/
JR00227700") is a client-side Angular route returning only the empty app
shell over plain HTTP. The real content comes from the same per-job detail
API as L&T/Aditya Birla, confirmed live and requiring no auth:
``GET /api/cp/rest/altone/cp/job/<code>/v2?part=basic,organisational,
descriprion,workflow,skill,qualification,certification,language,applied
&isReqId=false`` (the "descriprion" typo in the param name is
PeopleStrong's own across every tenant seen in this repo, reproduced
verbatim because the API requires it exactly) -- returns ``jobDescription``
(HTML) and ``CandidatePortalStartDate``.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests

_BASE = "https://bflcareers.peoplestrong.com"
_JOBS_API = f"{_BASE}/api/cp/rest/altone/cp/jobs/v1"
_DETAIL_API_TMPL = f"{_BASE}/api/cp/rest/altone/cp/job/{{code}}/v2"
_DETAIL_PARTS = (
    "basic,organisational,descriprion,workflow,skill,qualification,"
    "certification,language,applied"
)

_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}

# Exact body shape observed from the live page's own search POST.
_SEARCH_BODY = {
    "bandList": None,
    "gradeList": None,
    "bandIDList": [],
    "gradeIDList": [],
    "employeeCategoryLabelList": None,
}

_JOB_CODE_RE = re.compile(r"/job/detail/([^/?#]+)")

# Per-keyword pagination-wraparound guard (see repo contract): maps
# keyword -> the set of job ids seen on that keyword's own start=0 page.
_FIRST_PAGE_IDS: dict[str, set[str]] = {}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Bajaj Finance's PeopleStrong portal."""


def _normalise_location(hierarchy: str) -> str:
    """Turn 'India>MAHARASHTRA>West>Pune>Pune Corporate Office - Mantri>
    Megapolis' into 'Megapolis, Pune Corporate Office - Mantri, Pune, West,
    MAHARASHTRA, India'.

    The leading segment is always the real country (same reliable,
    hand-list-free signal as L&T's tenant on the same ATS vendor) -- no
    per-city normalization table is needed, matcher.py's own
    ``exclude_locations`` substring check does the real filtering (e.g.
    "Pune" anywhere in this string correctly excludes it).
    """
    parts = [p.strip() for p in (hierarchy or "").split(">") if p.strip()]
    if not parts:
        return ""
    return ", ".join(reversed(parts))


def _parse_datetime_to_date(raw: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM:SS.f' (or already-bare 'YYYY-MM-DD') -> 'YYYY-MM-DD'."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    date_part = raw.split(" ", 1)[0]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
        return date_part
    except ValueError:
        return ""


def _post_with_retry(url: str, timeout: int, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=_HEADERS, timeout=timeout, **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Bajaj Finance: 429 rate-limited on jobs search")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Bajaj Finance jobs search failed: {exc}") from exc
    raise RateLimitError(f"Bajaj Finance jobs search: no response -- {last_exc}")


def _get_with_retry(url: str, timeout: int, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout, **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Bajaj Finance: 429 rate-limited on job detail")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Bajaj Finance job detail fetch failed: {exc}") from exc
    raise RateLimitError(f"Bajaj Finance job detail fetch: no response -- {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Bajaj Finance jobs matching *keyword*.

    ``location`` is accepted for contract compatibility but not sent to the
    API (no reliable country-level location param was found on this
    tenant) -- matcher.py's own ``is_india_job()`` does the real India
    filtering against the ``location`` field this function returns, built
    from the ATS's own reliable per-posting country hierarchy.
    """
    params: dict[str, object] = {"offset": start, "limit": num}
    if keyword:
        params["searchString"] = keyword

    r = _post_with_retry(_JOBS_API, timeout, params=params, json=_SEARCH_BODY)
    data = r.json()
    raw_jobs = data.get("response") or []

    jobs: list[dict] = []
    ids_this_page: set[str] = set()
    for raw in raw_jobs:
        req_id = raw.get("requisitionId")
        if req_id is None:
            continue
        job_id = str(req_id)
        ids_this_page.add(job_id)
        jobs.append({
            "id": job_id,
            "title": (raw.get("jobTitle") or "").strip(),
            "location": _normalise_location(raw.get("locationHierarchyComplete", "")),
            "posting_date": _parse_datetime_to_date(raw.get("jobPostedDate", "")),
            "application_url": raw.get("jobDetailUrl") or "",
        })

    if start == 0:
        _FIRST_PAGE_IDS[keyword] = ids_this_page
    elif ids_this_page and _FIRST_PAGE_IDS.get(keyword) == ids_this_page:
        return []  # ATS silently replayed this keyword's first page

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description and posting date from PeopleStrong's
    detail API (the SPA's own detail page URL returns only an empty app
    shell over plain HTTP -- see module docstring).

    Returns (description_text, posting_date) where posting_date is
    YYYY-MM-DD.
    """
    m = _JOB_CODE_RE.search(application_url or "")
    if not m:
        return "", ""
    job_code = m.group(1)

    url = _DETAIL_API_TMPL.format(code=job_code)
    r = _get_with_retry(url, timeout, params={"part": _DETAIL_PARTS, "isReqId": "false"})
    data = r.json().get("response") or {}

    raw_desc = data.get("jobDescription") or ""
    text = re.sub(r"<[^>]+>", " ", raw_desc)
    text = html_mod.unescape(text)
    description = " ".join(text.split())

    posting_date = _parse_datetime_to_date(data.get("CandidatePortalStartDate", ""))

    return description, posting_date
