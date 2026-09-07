"""Fetches Larsen & Toubro's OWN corporate/conglomerate job listings via its
PeopleStrong "Candidate Portal" ATS (larsentoubrocareers.peoplestrong.com).

NOTE: this is L&T the parent engineering/construction/manufacturing
conglomerate hiring across its many "IC"/business units (L&T Energy, L&T
Semiconductor Technologies, L&T Precision Engineering, L&T Realty, L&T
Valves, Buildings & Factories, Heavy Civil Infrastructure, etc.) -- it is
DISTINCT from ``ltts_fetcher.py`` (L&T Technology Services) and
``ltimindtree_fetcher.py`` (LTIMindtree), both of which are already
onboarded as their own separately-listed companies with their own ATSes.

ATS discovery (live, 2026-09-07): larsentoubro.com/corporate/careers links
out to larsentoubrocareers.peoplestrong.com, an Angular SPA on PeopleStrong
-- an Indian HR-tech vendor not otherwise seen in this repo (a new ATS
pattern, not Workday/SuccessFactors/Taleo/Darwinbox/RippleHire/Zwayam).
Live network capture (clicking a location facet, then a job title) found
the real REST API behind the SPA, all served with NO Cloudflare/bot-
management wall and NO auth required -- confirmed reproducible via plain
``requests``:

  - Search: ``POST /api/cp/rest/altone/cp/jobs/v1?offset=<n>&limit=<n>
    &searchString=<kw>`` with a fixed, mostly-null JSON body (`{"bandList":
    null, "gradeList": null, "bandIDList": [], "gradeIDList": [],
    "employeeCategoryLabelList": null}` -- this exact shape is sent by the
    real page and is required; omitting it works too, but this fetcher
    matches the observed request exactly). Response:
    ``{"totalRecords": <int>, "response": [{requisitionId, jobTitle,
    jobPostedDate ("YYYY-MM-DD", already ISO), locationHierarchyComplete
    ("India>Karnataka>Bengaluru" or "Oman>..."/"Uzbekistan>..."/etc for
    overseas roles), jobDetailUrl, ...}]}``.
  - ``searchString`` genuinely narrows server-side (confirmed live 2026-09-
    07): empty/omitted = 1204 total India+overseas postings;
    ``zzznonsensequeryabc123`` = 0. BUT unlike most other RippleHire/J2W
    tenants in this repo, matching here is a literal case-insensitive
    SUBSTRING match against the title (not a tokenized OR/AND) -- e.g.
    "software engineer"=3, ".NET developer"=1, but "senior software
    engineer"=0 and "machine learning engineer"=0 even though "senior
    engineer" alone=14 and "engineer" alone=457 -- there is no live posting
    whose title contains the *exact* multi-word phrase for most of the
    standard keyword set. This is a genuine platform behavior (verified
    case-insensitivity: "SOFTWARE ENGINEER" returns the same 3 as
    "software engineer"), not a fetcher bug -- L&T's live board today is
    overwhelmingly civil/mechanical/electrical engineering and plant
    operations, with only a small "Digital Engineer - IoT and AI" / "HW
    Developer" / ".NET Developer" software-adjacent slice. Real matches
    will surface automatically as titles change; the strict substring
    behavior means this ATS is narrower than most peers here, not looser.
  - Pagination: ``offset``/``limit`` map directly onto this fetcher's
    ``start``/``num`` -- confirmed live an out-of-range offset (e.g.
    10000 against a real 457-result pool) returns an empty ``response``
    list rather than wrapping back to page 1, so ``matcher.py``'s own
    "stop on empty page" loop termination is safe on its own; a
    lightweight per-keyword ``_FIRST_PAGE_IDS`` guard is still kept here as
    a defensive backstop against a future regression. The API silently
    caps a single request's returned rows at 99 regardless of a larger
    requested ``limit`` (irrelevant in practice: matcher.py's own page size
    is 20).
  - No usable "country" or "India-only" param was found on this endpoint
    (only city-level ``worksite`` facet values like "Bengaluru"/"Sohar,
    Oman" were observed) -- India detection instead relies on the
    ``locationHierarchyComplete`` string's own leading "India>" segment,
    which is completely reliable (every posting carries a real country as
    its first hierarchy segment) and needs no hand-maintained city list,
    unlike most other fetchers in this repo.

Detail page: the SPA's own ``jobDetailUrl`` (e.g. ".../job/detail/
LNT_D_1848745") is a client-side Angular route that returns only the empty
app shell over plain HTTP -- the real content comes from a second API,
confirmed live via Playwright network capture while opening a job:
``GET /api/cp/rest/altone/cp/job/<code>/v2?part=basic,organisational,
descriprion,workflow,skill,qualification,certification,language,applied
&isReqId=false`` (note: "descriprion" is PeopleStrong's own typo in this
param name, not a mistake introduced here -- reproduced verbatim because
the API requires it) -- returns ``jobDescription`` (HTML) and
``CandidatePortalStartDate`` ("YYYY-MM-DD HH:MM:SS.f").
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests

_BASE = "https://larsentoubrocareers.peoplestrong.com"
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
    """Raised on 429 or persistent connection failure from L&T's PeopleStrong portal."""


def _normalise_location(hierarchy: str) -> str:
    """Turn 'India>Karnataka>Bengaluru' into 'Bengaluru, Karnataka, India'.

    The leading segment is always the real country (confirmed live across
    both India and overseas -- Oman/Uzbekistan/Saudi Arabia/UAE -- postings),
    so this is a reliable, hand-list-free signal for matcher.py's
    ``is_india_job()`` substring check -- unlike most other fetchers here,
    no per-city normalization table is needed.
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
                raise RateLimitError("L&T: 429 rate-limited on jobs search")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"L&T jobs search failed: {exc}") from exc
    raise RateLimitError(f"L&T jobs search: no response -- {last_exc}")


def _get_with_retry(url: str, timeout: int, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout, **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("L&T: 429 rate-limited on job detail")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"L&T job detail fetch failed: {exc}") from exc
    raise RateLimitError(f"L&T job detail fetch: no response -- {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of L&T jobs matching *keyword*.

    ``location`` is accepted for contract compatibility but not sent to the
    API (no reliable country-level location param was found on this
    tenant) -- matcher.py's own ``is_india_job()`` does the real India
    filtering against the ``location`` field this function returns, which
    is built from the ATS's own reliable per-posting country hierarchy.
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
