"""Fetches BMW TechWorks India job listings via its PeopleStrong "Candidate
Portal" ATS (careers-bmwtechworks.peoplestrong.com) -- the same Indian
HR-tech vendor already onboarded for L&T/Aditya Birla/Bajaj Finance
(`larsentoubro_fetcher.py`/`adityabirla_fetcher.py`/`bajaj_fetcher.py`), a
different tenant here.

Entity chosen (live investigation, 2026-09-13): the generic
``bmwgroup.jobs`` careers site (BMW Group's global AEM-based portal, no
third-party ATS domain visible in its SSR HTML or clientlib bundles -- its
"Job Finder" widget could not be reverse-engineered to a working REST
endpoint from plain HTTP within investigation time) is NOT the pipeline
used here. Instead, **BMW TechWorks India** -- BMW Group's dedicated India
engineering GCC, a joint venture with Tata Technologies, per its own
distinct careers portal at ``careers-bmwtechworks.peoplestrong.com`` --
is the pipeline onboarded, confirmed live to be BMW's real, actively used
India SDE hiring surface (Digital Car / ADAS / AI & ML / Digital Product
Engineering org units all appear in real postings).

BMW TechWorks India is HQ'd in Pune, and Pune is this repo's own
`exclude_locations` entry -- explicitly checked against the "Pune-only
GCC" exclusion precedent (John Deere/Mercedes-Benz R&D/VW Group Technology
Solutions) before onboarding. It clears the bar: of a 92-job India pool
found live, 74 are Pune (excluded) but **16 are genuinely non-excluded
Bengaluru** postings (plus 1 Chennai, already excluded, and 1
"Pune OR Bengaluru" multi-location row) -- including real AI/ML titles
seen live ("Agentic AI engineer - AIML", "ML Ops -AWS Engineer - AIML Data
Enrichment", "ADAS ML/Data Analytics", "Web App Full Stack Developer GenAI
& Knowledge Systems") and a genuine ADAS/C++ software-engineering cluster,
not just plant/manufacturing roles.

Same REST API shape as Bajaj Finance/L&T/Aditya Birla, confirmed live and
reproducible via plain ``requests`` with no auth/session/cookies:
``POST /api/cp/rest/altone/cp/jobs/v1?offset=<n>&limit=<n>&searchString=<q>``
with the same fixed, mostly-null JSON body. One quirk specific to this
tenant not seen on Bajaj/L&T: ``searchString=""`` returns
``totalRecords: 0`` (this tenant requires a non-empty query), and the
search itself does loose OR-of-tokens matching rather than a real phrase
filter (e.g. "senior software engineer" returns 74 of the ~92-job pool --
almost everything, not a real narrowing) -- too noisy to rely on for
per-keyword queries, same caveat as `persistent_fetcher.py`'s Zwayam OR
match. So this fetcher does what Persistent does: runs a fixed set of
broad queries once, unions the results by ``jobCode`` into an in-module
India-only cache, and re-serves that same cache (paginated) for every
keyword call the generic runner issues -- correctly registered in
`_IGNORES_KEYWORDS`.

Each list-response job has ``requisitionId: null`` on this tenant (unlike
Bajaj's), so ``jobCode`` (e.g. "BTWI/AAE-B/1794462") is the real per-job
identifier; ``jobDetailUrl`` embeds the same code with `/` -> `_` (e.g.
".../job/detail/BTWI_AAE-B_1794462").

Detail page: the SPA's own ``jobDetailUrl`` is a client-side Angular route
returning only the empty app shell over plain HTTP. The real content comes
from the same per-job detail API as Bajaj/L&T, confirmed live and requiring
no auth: ``GET /api/cp/rest/altone/cp/job/<code>/v2?part=basic,
organisational,descriprion,workflow,skill,qualification,certification,
language,applied&isReqId=false`` (the "descriprion" typo is PeopleStrong's
own across every tenant seen in this repo, reproduced verbatim because the
API requires it exactly) -- returns ``jobDescription`` (HTML) and
``CandidatePortalStartDate``.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests

_BASE = "https://careers-bmwtechworks.peoplestrong.com"
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

_SEARCH_BODY = {
    "bandList": None,
    "gradeList": None,
    "bandIDList": [],
    "gradeIDList": [],
    "employeeCategoryLabelList": None,
}

# This tenant's `searchString` does a noisy OR-of-tokens match, not a real
# phrase filter (see module docstring), and requires a non-empty value.
# These broad queries are chosen to maximize coverage of the small (~90-job)
# pool in one cache-fill pass -- overlapping on purpose, deduped by jobCode.
_CACHE_FILL_QUERIES = (
    "a", "e", "i", "s", "r", "t", "l", "c", "d", "m", "o", "n",
    "engineer", "developer", "manager", "analyst", "architect",
    "specialist", "lead", "expert", "test", "quality",
)

_JOB_CODE_URL_RE = re.compile(r"/job/detail/([^/?#]+)")

_cache: list[dict] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from BMW TechWorks India's PeopleStrong portal."""


def _normalise_location(hierarchy: str) -> str:
    """'India>Karnataka>Bengaluru>Bengaluru' -> 'Bengaluru, Bengaluru, Karnataka, India'."""
    parts = [p.strip() for p in (hierarchy or "").split(">") if p.strip()]
    if not parts:
        return ""
    return ", ".join(reversed(parts))


def _parse_datetime_to_date(raw: str) -> str:
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
                raise RateLimitError("BMW TechWorks India: 429 rate-limited on jobs search")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"BMW TechWorks India jobs search failed: {exc}") from exc
    raise RateLimitError(f"BMW TechWorks India jobs search: no response -- {last_exc}")


def _get_with_retry(url: str, timeout: int, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout, **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("BMW TechWorks India: 429 rate-limited on job detail")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"BMW TechWorks India job detail fetch failed: {exc}") from exc
    raise RateLimitError(f"BMW TechWorks India job detail fetch: no response -- {last_exc}")


def _fetch_query_all_pages(query: str, timeout: int) -> list[dict]:
    jobs: list[dict] = []
    offset = 0
    limit = 50
    while True:
        r = _post_with_retry(
            _JOBS_API,
            timeout,
            params={"offset": offset, "limit": limit, "searchString": query},
            json=_SEARCH_BODY,
        )
        data = r.json()
        total = data.get("totalRecords") or 0
        page = data.get("response") or []
        jobs.extend(page)
        if not page or offset + limit >= total:
            break
        offset += limit
    return jobs


def _fill_cache(timeout: int) -> list[dict]:
    global _cache
    _cache = []  # set before the loop so a mid-fill failure doesn't retry-storm
    by_code: dict[str, dict] = {}
    for query in _CACHE_FILL_QUERIES:
        for raw in _fetch_query_all_pages(query, timeout):
            job_code = raw.get("jobCode")
            if not job_code or job_code in by_code:
                continue
            loc = raw.get("locationHierarchyComplete", "") or ""
            if not loc.lower().startswith("india"):
                continue
            title = (raw.get("jobTitle") or "").strip()
            if not title:
                continue
            detail_url = raw.get("jobDetailUrl") or ""
            by_code[job_code] = {
                "id": job_code,
                "title": title,
                "location": _normalise_location(loc),
                "posting_date": raw.get("jobPostedDate", "") if _looks_like_date(raw.get("jobPostedDate", "")) else "",
                "application_url": detail_url,
            }
    _cache = list(by_code.values())
    return _cache


def _looks_like_date(s: str) -> bool:
    try:
        datetime.strptime((s or "").strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of BMW TechWorks India jobs.

    ``keyword``/``location`` are accepted for contract compatibility but not
    used to filter server-side -- this tenant's `searchString` is a noisy
    OR-of-tokens match (see module docstring), so the full India-only pool
    is cached once and re-served (paginated) for every call; the generic
    runner is told this via `_IGNORES_KEYWORDS` in company_registry.py.
    """
    jobs = _cache if _cache is not None else _fill_cache(timeout)
    return jobs[start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description and posting date from PeopleStrong's
    detail API (the SPA's own detail page URL returns only an empty app
    shell over plain HTTP -- see module docstring).

    Returns (description_text, posting_date) where posting_date is
    YYYY-MM-DD.
    """
    m = _JOB_CODE_URL_RE.search(application_url or "")
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
