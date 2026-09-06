"""
Cyient job fetcher — Zwayam ATS (public.zwayam.com), reached via an Angular
"careers site builder" front-end at careers.cyient.com.

Cyient's branded careers page (`careers.cyient.com` -> redirects to
`careers.cyient.com/cyient/`) looks like a bespoke marketing site at a
glance, but it is a minified Angular SPA (`main.<hash>.js`) with no server-
rendered job content at all. Live investigation (2026-09-06) traced the real
backing platform in stages, not assumed:
1. The page's own response headers carry a `content-security-policy` with
   `frame-ancestors ... *.openings.co *.zwayam.com ...` — an unusual, very
   specific allow-list that named the real vendor before any JS was read.
2. The rendered HTML embeds a literal `https://webhooks.naukri.com/zwayam?
   conversation=cyient_root` reference — Zwayam is a Naukri/Info Edge-owned
   ATS product, confirming the CSP hint.
3. `main.<hash>.js` contains a plaintext runtime `environment` object:
   `DOMAIN:"careers.cyient.com", APIENDPOINT:"https://public.zwayam.com/",
   COMPANYID:"MTU0ODY="` (base64 for the plain company id `15486`) — the
   actual API host and tenant identifier, hardcoded per-tenant at build time
   (no auth/session token needed to call it).

Verified via direct requests against the live API (not assumed):
- Search: `POST public.zwayam.com/jobs/search`, body
  (`application/x-www-form-urlencoded` — tolerates urlencoded even though
  the Angular client sends multipart/form-data; both were tested directly)
  with `companyId` (the base64 string above, sent *undecoded* — matches
  what the Angular client itself does), `domain` ("careers.cyient.com"),
  and `filterCri` (a JSON string: `paginationStartNo`, `selectedCall`:
  "sort", `sortCriteria`, `anyOfTheseWords` for the keyword). No auth
  header/cookie/tenant-group header is required — confirmed by calling it
  cold with none of those and getting real data back.
- `anyOfTheseWords` genuinely narrows server-side (132/132 for an empty
  query, 3/132 for ".NET", 0/132 for a nonsense token) — it is an "any of
  these words" OR-of-tokens match, not a phrase match ("software engineer"
  alone returns 85/132, i.e. every posting with "engineer" anywhere in its
  title, mechanical/hardware included).
- The server always returns exactly 10 hits per call regardless of any
  page-size-shaped field tried (`pageSize`, `size`, `noOfRecords`,
  `resultsPerPage`, `recordsPerPage` — none changed the count); only
  `paginationStartNo` (a genuine 0-based record offset, not a page index —
  confirmed non-overlapping results at offset 0 vs. 10) moves the window.
- Total board size is small and stable: 132 jobs globally, 96 tagged India
  (`_source.text3`, the country facet's backing field — its per-value sums
  exactly match the response's own `facets.Country` aggregation, confirmed
  by direct count).

**Deliberately ignores `keyword` — registered in `_IGNORES_KEYWORDS` in
company_registry.py.** The API's own pagination has no independent-of-
filter cursor semantics usable by matcher.py's `start`-accumulation loop:
`start` there advances by `len(page)`, i.e. by however many jobs *this*
fetcher decided to return, not by how many raw records the underlying API
actually consumed. If jobs were filtered down (e.g. to India only, or by a
keyword) *before* being counted into `start`, a run of several consecutive
non-matching raw records would either stall `start` (risking an infinite
while-loop in matcher.py) or silently skip real records once pagination
resumed at the wrong raw offset. Given the whole board is only ~130 jobs,
this fetcher instead fetches the *entire* raw list once per process
(looping `paginationStartNo` in the API's own fixed steps of 10 until an
empty page), filters to India, and caches the result at module scope;
`fetch_jobs()` then just slices that cached, already-correct list by the
caller's `start`/`num` — the same "empty keyword fetches everything; title/
skill filters handle the rest" choice already made for MetLife/Infosys/
HealthEdge in this repo, applied here for pagination-correctness reasons
rather than a broken/absent keyword param (this tenant's keyword filter
does work).

**New gotcha, not previously in this repo's Key Bugs table: the search
index's own `city`/`locationSeparatedbySlash`/`locationDisplayForManageJobs`
fields can be stale and disagree with the job's real, current location.**
Direct cross-check (2026-09-06): of the 132 raw jobs, 21 (~16%) have a
`city` value naming a *different actual city* than the top-level `location`
field on the same document (e.g. job 1141049 "Senior software developer":
`city: "Noida, India"` vs. `location: "Hyderabad, Telangāna, India"`). Four
of these were independently checked against the authoritative per-job
detail endpoint (`jobs-service/v1/jobs/careersite`, the same call the real
job-view page makes) and **all four times the detail endpoint's own
`location` field matched the search response's `location`/`locAgg` field,
never `city`** — i.e. `city` (and the derived `locationSeparatedbySlash`/
`locationDisplayForManageJobs` fields, always identical to `city`) is the
stale one, likely left over from whichever office a requisition was
originally opened under before being reassigned, while `location`/`locAgg`
reflect the current, real value. This fetcher therefore builds every job's
`location` field from `_source["location"]` only — never `city` — which
also sidesteps needing a `_pick_city_segment`-style joint-location handler
(no comma/slash-joined multi-city strings were observed in this field for
any of the 96 India jobs; all are clean single "City, State, India" or
"City, India" strings).

Job descriptions are NOT inline in the search response in a directly usable
form: the `_source.jdSkillsKnown`/`desiredSkill`/`skillSet` fields on the
search index are real but inconsistently populated (empty for some genuine
.NET postings, e.g. job 1141002, populated for others, e.g. a QGIS/Python
posting) and the ES index's `role` field is a generic, HR-job-family
boilerplate paragraph shared verbatim across unrelated postings (confirmed:
two different Noida jobs return byte-identical `role` text) — not real
per-posting content, so it is deliberately excluded, same reasoning as
Eurofins/every other fetcher here that drops a generic "company
description" section. The real, reliable per-posting JD lives in the
detail endpoint's `longDescription` (HTML) field, fetched separately by
`fetch_job_description()`; `skillSet`/`desiredSkill` from that same detail
call are appended when non-empty as a defensive extra (harmless — they are
plain comma-joined skill names, not HTML).

Live-verified 2026-09-06: 96 India postings, but Cyient's core business
really is engineering R&D services (aerospace/rail/semiconductor/energy) as
the task brief anticipated — the pool is overwhelmingly CATIA/DFT/stress/
analog-layout/hydraulics/wiring-harness/RTL-design titles that correctly
fail `title_family` (no "engineer"/"developer" family phrase they match is
software-specific) or `exclude_terms` (mechanical/electrical/hardware/
embedded). Only a handful of titles pass `title_family` at all: "Software
Engineer" (Noida) and "Senior software developer" (Hyderabad — see the
location gotcha above) are real, current `.NET / C#` matches (their
`longDescription` explicitly names ".NET, C#"/"​.Net Core, Entity
Framework"). "Senior Software Engineer - FMS" (Bengaluru, an avionics
Flight Management System role) and "QGIS Python Developer" (Hyderabad) also
pass `title_family` but their real JDs name C/C++ embedded and QGIS/PyQGIS
respectively — neither is a genuine `.NET / C#` or `AI / ML / Python`
primary-skill hit (the QGIS JD's one "AI/ML-based geospatial solutions is
an added advantage" line is a soft mention, not a hard LangChain/RAG/vector-
db term), so both are correctly excluded downstream. No real current
`AI / ML / Python` match exists in this pool — a genuine current zero, not
a fetcher defect (see the onboarding report for full counts).
"""
from __future__ import annotations

import base64
import html as _html_mod
import json
import re
import time
from datetime import datetime, timezone

import requests

_SEARCH_URL = "https://public.zwayam.com/jobs/search"
_DETAIL_URL = "https://public.zwayam.com/jobs-service/v1/jobs/careersite"
_JOBVIEW_BASE = "https://careers.cyient.com/cyient/jobview"

_COMPANY_ID_B64 = "MTU0ODY="  # sent as-is to /jobs/search, matches Angular client
_COMPANY_ID_PLAIN = base64.b64decode(_COMPANY_ID_B64).decode()  # "15486"
_DOMAIN = "careers.cyient.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.cyient.com/cyient/",
}

# Populated once per process by _fetch_all_india_jobs(); see module docstring
# for why this fetcher deliberately ignores `keyword` and caches the full,
# already India-filtered list instead of trusting matcher.py's start/num
# accumulation against the raw (unfiltered) API cursor.
_all_jobs_cache: list[dict] | None = None

# job id (str) -> (description, posting_date), populated by fetch_job_description
_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent failure from the Zwayam API."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_epoch_ms(raw) -> str:
    """1783920645000 -> '2026-07-13'."""
    if not raw:
        return ""
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def _post_with_retry(url: str, *, data=None, json_body=None, timeout: int, label: str):
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            if json_body is not None:
                r = requests.post(url, headers=_HEADERS, json=json_body, timeout=timeout)
            else:
                r = requests.post(url, headers=_HEADERS, data=data, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Cyient {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Cyient {label} failed: {exc}") from exc
    raise RateLimitError(f"Cyient {label}: no response — {last_exc}")


def _search_page(start: int, timeout: int) -> list[dict]:
    """One raw page (always <=10 hits) from the Zwayam search index."""
    filter_cri = {
        "paginationStartNo": start,
        "selectedCall": "sort",
        "sortCriteria": {"name": "modifiedDate", "isAscending": False},
        "anyOfTheseWords": "",
    }
    data = {
        "companyId": _COMPANY_ID_B64,
        "domain": _DOMAIN,
        "filterCri": json.dumps(filter_cri),
    }
    r = _post_with_retry(_SEARCH_URL, data=data, timeout=timeout, label="search")
    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Cyient search returned non-JSON body: {exc}") from exc
    return (payload.get("data") or {}).get("data") or []


def _fetch_all_india_jobs(timeout: int) -> list[dict]:
    """Fetch and cache the full India-tagged job pool exactly once per process.

    See module docstring for why: matcher.py's pagination cursor only sees
    however many jobs we hand back each call, so filtering to India *before*
    counting into that cursor (rather than fetching everything once and
    slicing) would risk either stalling or silently skipping real records.
    """
    global _all_jobs_cache
    if _all_jobs_cache is not None:
        return _all_jobs_cache

    jobs: list[dict] = []
    start = 0
    while start < 2000:  # defensive cap; real board is ~130 jobs total
        raw = _search_page(start, timeout)
        if not raw:
            break
        for hit in raw:
            src = hit.get("_source") or {}
            job_id = str(src.get("id") or "").strip()
            title = (src.get("jobTitle") or "").strip()
            country = (src.get("text3") or "").strip()
            location = (src.get("location") or "").strip()
            if not job_id or not title or country.lower() != "india":
                continue
            if "india" not in location.lower():
                continue
            job_url = (src.get("jobUrl") or "").strip()
            if not job_url:
                continue
            jobs.append({
                "id": job_id,
                "title": title,
                "location": location,
                "posting_date": _parse_epoch_ms(src.get("createdDate")),
                "application_url": f"{_JOBVIEW_BASE}/{job_url}",
            })
        start += len(raw)

    _all_jobs_cache = jobs
    return jobs


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of Cyient's cached, India-only job pool.

    `keyword` and `location` are intentionally ignored — see module
    docstring ("_IGNORES_KEYWORDS"). The full pool (currently 96 India jobs)
    is fetched and filtered once per process on first call; every
    subsequent call (across every keyword/location matcher.py iterates)
    just slices the same cached list, so `start`/`num` behave as plain,
    correct list indexing rather than a live API cursor.
    """
    all_jobs = _fetch_all_india_jobs(timeout)
    return all_jobs[start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the real per-posting description via the Zwayam detail endpoint.

    Concatenates the HTML `longDescription` (the actual JD body) with the
    plain-text `skillSet`/`desiredSkill` fields when present (defensive —
    these are sometimes the more explicit signal, see module docstring's
    QGIS example). The generic `role` field (a boilerplate job-family
    paragraph, confirmed byte-identical across unrelated postings) is
    deliberately excluded.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    job_url = application_url.rstrip("/").split(f"{_JOBVIEW_BASE}/")[-1]
    body = {
        "jobUrl": job_url,
        "companyID": _COMPANY_ID_PLAIN,
        "externalSource": "CAREERSITE",
        "campusURL": "empty",
    }
    r = _post_with_retry(_DETAIL_URL, json_body=body, timeout=timeout, label="description")
    try:
        detail = r.json()
    except ValueError:
        return "", ""

    parts = []
    long_desc = detail.get("longDescription") or ""
    if long_desc:
        parts.append(_strip_html(long_desc))
    for key in ("skillSet", "desiredSkill"):
        val = (detail.get(key) or "").strip()
        if val:
            parts.append(val)
    description = " ".join(parts)

    posting_date = _parse_epoch_ms(detail.get("createdDate"))

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
