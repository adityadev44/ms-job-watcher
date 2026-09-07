"""Fetches Aditya Birla Group's own job listings via careers.adityabirla.com's
first-party Next.js API, which itself proxies the group's PeopleStrong
"Candidate Portal" tenant (abgcareers.peoplestrong.com) -- the same ATS
vendor as ``larsentoubro_fetcher.py``, but exposed here through a
same-origin wrapper with a friendlier response shape and an inline job
description (no separate detail fetch needed in the common case).

ATS discovery (live, 2026-09-07): adityabirla.com's own careers link
(https://www.adityabirla.com/en/careers) is mostly a marketing page;
web search found the group's real careers portal at
careers.adityabirla.com, a Next.js app whose "Search Jobs" page
(``/job-search``) calls its own same-origin API:

    GET https://careers.adityabirla.com/api/v3/jobs
        ?sortBy=new&offset=<n>&limit=<n>&searchString=<kw>

Live Playwright network capture of that call found it requires a bearer
token (``Authorization: Bearer <64-hex-char token>``) that is embedded
client-side (not issued via any separate session/token endpoint observed
in the request sequence) -- confirmed live to be a static, reusable value:
a bare ``requests.get`` with no cookies/session, just that header plus a
``Referer: https://careers.adityabirla.com/job-search``, returns real data.
**Gotcha**: an invalid/missing token does NOT come back as a real HTTP 401
-- the endpoint always returns HTTP 200 with a JSON body
``{"status": 401, "message": "Unauthorized access"}`` instead (confirmed
live by testing both a bad token and no ``Authorization`` header at all).
This fetcher therefore checks the JSON body's own ``status`` field, not
just the HTTP status code, and raises ``RateLimitError`` on a soft-401 so a
future token rotation by ADB fails loudly instead of silently returning
"zero jobs" forever. If that ever happens, refresh ``_BEARER_TOKEN`` below
by loading https://careers.adityabirla.com/job-search live and reading the
``authorization`` request header off the ``/api/v3/jobs`` call.

Response shape (differs from L&T's raw PeopleStrong API despite the same
backend): ``{"status": 200, "count": <returned>, "totalJobs": <int>,
"data": [{id, jobTitle, jobCode, jobPostedDate ("2026-09-06T00:00:00.000Z",
ISO not bare date), locationHierarchyComplete ("India>Madhya Pradesh>
Indore, Madhya Pradesh"), jobDetailUrl ("https://abgcareers.peoplestrong
.com/job/detail/ABG111889"), jobDescription (HTML, already INLINE),
...}]}``. Note the sub-field ``requisitionId`` on this wrapper is always
``0`` (unlike L&T's raw API) -- the real, unique identifier here is the
top-level ``id``.

``searchString`` genuinely narrows server-side (confirmed live 2026-09-07):
``zzznonsensequeryabc123``→0; real terms differ ("software engineer"=4,
"senior software engineer"=2, "AI engineer"=2, ".NET developer"/"C#
developer"/"dot net"/"angular"/"machine learning engineer"/"python
developer"/"generative ai engineer"=0 right now) -- same literal
substring-match behavior as L&T's own tenant (case-insensitive: "SOFTWARE
ENGINEER" and "software engineer" return the same count), consistent with
both running on PeopleStrong.

**Pagination gotcha** (verified live, keyword-dependent): the API's own
``totalJobs`` figure can be dramatically larger than what is actually
retrievable by paging through ``offset``/``limit`` -- e.g. searching
"manager" reports ``totalJobs: 1407`` but ``offset=40`` already returns an
empty ``data`` list, and the unfiltered (no ``searchString``) firehose
reports ``totalJobs: 2513`` while only ~138 records are actually walkable
before pages go empty. This never manifests as wraparound/duplicate data
(confirmed: it fails CLEANLY with an empty list, never repeats page 1) --
just an early stop -- so ``matcher.py``'s ordinary "stop paginating on an
empty page" behavior already handles it safely with no special-case code
needed here. In practice this repo's default keyword list only ever
returns single-digit counts on this tenant today, so the cap is never
actually hit.

Detail/description: ``jobDescription`` already arrives inline in the search
response, cached here per ``application_url`` during ``fetch_jobs`` so
``fetch_job_description`` normally needs no extra network call at all (same
"served from the list-fetch cache" idiom as ``darwinbox_fetcher.py``). If
called for a URL that was never seen via ``fetch_jobs`` in this process
(e.g. an isolated/unit-style call), it falls back to the underlying
PeopleStrong tenant's own per-job detail API -- confirmed live to work with
no auth needed, same shape as L&T's fallback:
``GET https://abgcareers.peoplestrong.com/api/cp/rest/altone/cp/job/<code>/
v2?part=basic,organisational,descriprion,workflow,skill,qualification,
certification,language,applied&isReqId=false`` (the "descriprion" typo in
the param name is PeopleStrong's own and must be reproduced verbatim).
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests

_BASE = "https://careers.adityabirla.com"
_JOBS_API = f"{_BASE}/api/v3/jobs"
_JOB_SEARCH_PAGE = f"{_BASE}/job-search"

# Static bearer token embedded in careers.adityabirla.com's own client JS
# bundle -- confirmed live to be reusable via plain requests with no
# session/cookies. See module docstring for how to refresh this if ADB
# ever rotates it (the API fails with a soft-401 JSON body, not HTTP 401).
_BEARER_TOKEN = "9f12ab0e7c6c9d65e9dc74b44f19a6a4c5c03861df6eb0fbd10ff4f4f9cd0349"

# PeopleStrong tenant backing this wrapper -- used only as a description
# fallback when a job wasn't already seen (and cached) via fetch_jobs.
_PS_BASE = "https://abgcareers.peoplestrong.com"
_PS_DETAIL_API_TMPL = f"{_PS_BASE}/api/cp/rest/altone/cp/job/{{code}}/v2"
_PS_DETAIL_PARTS = (
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
    "Authorization": f"Bearer {_BEARER_TOKEN}",
    "Referer": _JOB_SEARCH_PAGE,
}

_JOB_CODE_RE = re.compile(r"/job/detail/([^/?#]+)")

# Inline-description cache filled by fetch_jobs, keyed by application_url.
_description_cache: dict[str, tuple[str, str]] = {}

# Per-keyword pagination-wraparound guard (see repo contract): maps
# keyword -> the set of job ids seen on that keyword's own start=0 page.
_FIRST_PAGE_IDS: dict[str, set[str]] = {}


class RateLimitError(Exception):
    """Raised on 429, a soft-401 (bad/expired bearer token), or persistent failure."""


def _normalise_location(hierarchy: str) -> str:
    """Turn 'India>Madhya Pradesh>Indore, Madhya Pradesh' into
    'Indore, Madhya Pradesh, India' -- the leading segment is always the
    real country (same reliable, hand-list-free signal as L&T's tenant).
    Some ADB postings redundantly repeat the state name inside the city
    segment (e.g. "Indore, Madhya Pradesh" one level below "Madhya
    Pradesh"); that exact repeated suffix is trimmed for a cleaner string,
    but this is cosmetic only -- the raw hierarchy already carries a
    reliable "India" substring either way.
    """
    parts = [p.strip() for p in (hierarchy or "").split(">") if p.strip()]
    if not parts:
        return ""
    cleaned = []
    for i, part in enumerate(parts):
        if i > 0:
            suffix = f", {parts[i - 1]}"
            if part.lower().endswith(suffix.lower()):
                part = part[: -len(suffix)].strip()
        cleaned.append(part)
    return ", ".join(reversed(cleaned))


def _parse_iso_date(raw: str) -> str:
    """Convert '2026-09-06T00:00:00.000Z' or '2026-09-06 00:00:00.0' -> '2026-09-06'."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    date_part = raw.split("T", 1)[0].split(" ", 1)[0]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
        return date_part
    except ValueError:
        return ""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _get_with_retry(url: str, timeout: int, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout, **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Aditya Birla: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Aditya Birla request failed: {exc}") from exc
    raise RateLimitError(f"Aditya Birla request: no response -- {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Aditya Birla Group jobs matching *keyword*.

    ``location`` is accepted for contract compatibility but not sent to the
    API (no location param was found on this tenant) -- matcher.py's own
    ``is_india_job()`` does the real India filtering against the
    ``location`` field this function returns.
    """
    params: dict[str, object] = {"sortBy": "new", "offset": start, "limit": num}
    if keyword:
        params["searchString"] = keyword

    r = _get_with_retry(_JOBS_API, timeout, params=params)
    data = r.json()

    if data.get("status") not in (200, None):
        # The API answers a bad/expired bearer token with HTTP 200 and a
        # JSON {"status": 401, ...} body rather than a real HTTP 401/403 --
        # treat that the same as a hard failure (see module docstring).
        raise RateLimitError(
            f"Aditya Birla: API returned status={data.get('status')} "
            f"({data.get('message')}) -- bearer token may need refreshing"
        )

    raw_jobs = data.get("data") or []

    jobs: list[dict] = []
    ids_this_page: set[str] = set()
    for raw in raw_jobs:
        raw_id = raw.get("id")
        if raw_id is None:
            continue
        job_id = str(raw_id)
        ids_this_page.add(job_id)
        application_url = raw.get("jobDetailUrl") or ""
        posting_date = _parse_iso_date(raw.get("jobPostedDate", ""))
        jobs.append({
            "id": job_id,
            "title": (raw.get("jobTitle") or "").strip(),
            "location": _normalise_location(raw.get("locationHierarchyComplete", "")),
            "posting_date": posting_date,
            "application_url": application_url,
        })
        if application_url and raw.get("jobDescription"):
            _description_cache[application_url] = (
                _strip_html(raw["jobDescription"]),
                posting_date,
            )

    if start == 0:
        _FIRST_PAGE_IDS[keyword] = ids_this_page
    elif ids_this_page and _FIRST_PAGE_IDS.get(keyword) == ids_this_page:
        return []  # ATS silently replayed this keyword's first page

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description_text, posting_date) for *application_url*.

    Served from the inline-description cache filled by ``fetch_jobs`` when
    available; otherwise falls back to the underlying PeopleStrong
    tenant's own per-job detail API (see module docstring).
    """
    cached = _description_cache.get(application_url)
    if cached is not None:
        return cached

    m = _JOB_CODE_RE.search(application_url or "")
    if not m:
        return "", ""
    job_code = m.group(1)

    url = _PS_DETAIL_API_TMPL.format(code=job_code)
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": _HEADERS["User-Agent"]},
                params={"part": _PS_DETAIL_PARTS, "isReqId": "false"},
                timeout=timeout,
            )
            if r.status_code == 429:
                raise RateLimitError(f"Aditya Birla description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Aditya Birla description fetch failed: {exc}") from exc

    if r is None:
        return "", ""

    data = r.json().get("response") or {}
    description = _strip_html(data.get("jobDescription", ""))
    posting_date = _parse_iso_date(data.get("CandidatePortalStartDate", ""))
    return description, posting_date
