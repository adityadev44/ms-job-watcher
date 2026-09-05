"""Fetches The Hartford job listings via the Workday public REST API.

The Hartford's branded careers front-end (`www.jointhehartford.com`) is a
TalentBrew skin — same vendor/shape already seen at Boeing/Disney/AstraZeneca
in this repo (see PLAYBOOK "A single real ATS backend can sit behind
multiple different-looking public frontends"). Live inspection of the
rendered India-location listing page (2026-09-05) found a
`thehartford.wd5.myworkdayjobs.com/Careers_External/login` link embedded in
the page HTML, confirming the real backing ATS is Workday, tenant
"thehartford", site "Careers_External". Confirmed directly (not just a
URL-shape guess): a plain unauthenticated POST to
`/wday/cxs/thehartford/Careers_External/jobs` returns real `jobPostings`
JSON with no Playwright/session needed — same CXS REST pattern as every
other Workday tenant in this repo (Wells Fargo, Genpact, SimCorp, Nvidia,
etc.).

The India-specific engineering presence is a real, recently-launched GCC:
public reporting (Feb 2026) confirms The Hartford opened a ~1,200-seat
technology centre in Hyderabad's Financial District. This lines up exactly
with the live data: every India posting's `locationsText` is the single
literal site name "India GCC-Puppalaguda Village" (Puppalaguda is a
neighbourhood in the Financial District) -- there is exactly one India
office, no other cities, no ambiguous multi-site "N Locations" postings at
all in the India-filtered result set (verified by paging through all 33
postings and checking every `locationsText` value is identical).

Verified via direct A/B requests against the live API (2026-09-05):
- The `locationCountry` facet is a genuine, reliable server-side India
  filter. Its WID (`c4f78be1a8f14da0ab49ce1162348a5e`) is the same shared
  cross-tenant India GUID already seen at Fidelity/Wells Fargo/Citi/Northern
  Trust/MUFG/Accenture (a coincidence of Workday's own shared reference
  data, not something specific to this fetcher). Applying it returns
  exactly 33 jobs, all genuinely India (matches the 33-count
  `locationRegionStateProvince` "Telangana" facet value one-for-one, i.e.
  zero non-Telangana leakage).
- `searchText` (keyword) genuinely narrows server-side: empty query -> 33,
  "software engineer" -> 22, ".NET" -> 11, "python" -> 16, "machine learning
  engineer" -> 2, a nonsense token -> 0. NOT registered in
  `_IGNORES_KEYWORDS`.
- `limit` HTTP 400s above 20 (tested: 20 -> 200 OK, 50/100 -> 400) -- same
  quirk as MUFG/Nvidia. Clamped defensively.
- Same pagination wraparound bug as MUFG/UBS/Nvidia/Pfizer/Walmart/SimCorp:
  requesting `offset` at or past the true per-keyword total does NOT return
  an empty page -- it silently replays page 1's exact postings forever
  (verified live: offset=33/40/60 against the unfiltered India facet, total
  33, all return the identical 20 page-1 job IDs; only offset=20 correctly
  returns the real last partial page, 13 jobs). Same fix as those tenants:
  memoize each keyword's page-1 first job ID and treat its reappearance on
  a later page as "no more results".

Location handling is simpler than most Workday tenants in this repo: every
genuine India posting's `locationsText` already contains the literal
substring "India" ("India GCC-Puppalaguda Village"), so matcher.py's
`is_india_job()` needs no client-side append/rewrite at all (still guarded
defensively in case a future posting omits it, same safety net as every
sibling fetcher).

Real matches confirmed on both tracks from live description fetches (not
just title text): "INDStaff Software Engineer - AI" (R2626213) explicitly
names LangChain, AutoGen, CrewAI, LlamaIndex, RAG pipelines, prompt
engineering, vector databases (Pinecone, ChromaDB), and "C#/.NET" as an
accepted secondary language alongside Python -- a genuine `AI / ML /
Python` hit. "Staff Software Engineer/AI-ML" and "Staff Engineer/AI-ML
Engineer" are two more explicitly AI/ML-titled postings in the same pool.
The board also carries several ".NET"-keyword-narrowed titles (Amazon
Connect Developer, Informatica/data-integration roles) whose descriptions
need checking individually -- see the onboarding report for the exact
verified count.

Job descriptions are NOT inline in the search response -- fetched from the
same Workday CXS JSON detail endpoint
(`GET /wday/cxs/thehartford/Careers_External/job/{externalPath}`), same
shape as every other Workday tenant here. `jobDescription` is HTML; stripped
via BeautifulSoup. `startDate` in the detail response is already
YYYY-MM-DD.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://thehartford.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/thehartford/Careers_External/jobs"
_JOB_BASE = f"{_BASE_URL}/Careers_External"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/thehartford/Careers_External"

# Shared cross-tenant Workday India GUID (also seen at Fidelity/Wells Fargo/
# Citi/Northern Trust/MUFG/Accenture/Nvidia's docstring note) -- confirmed
# live for this tenant specifically via the `locationCountry` facet listing
# ("India", count 33), not assumed from another tenant.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Tenant HTTP 400s on any limit above 20 (verified live: 20 -> 200, 50/100 ->
# 400), same quirk as MUFG/Nvidia.
_PAGE_SIZE = 20
_MAX_LIMIT = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_JOB_BASE}",
}

# Pagination wraps around past the real result count for a keyword (same bug
# class as UBS BrassRing / MUFG / Nvidia / Pfizer / Walmart / SimCorp):
# requesting offset >= total does NOT return an empty jobPostings array --
# it silently re-returns page 1 verbatim. Track each keyword's first-page
# first job ID and treat a repeat of it on a later page as "no more
# results" so matcher.py's `if not page: break` pagination loop actually
# terminates instead of re-fetching the same page until max_listings is
# exhausted.
_page1_first_id: dict[str, str] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper -- Workday returns relative strings like "Posted 3 Days Ago"
# ---------------------------------------------------------------------------


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


def _post_with_retries(body: dict, timeout: int):
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(_SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("The Hartford search: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"The Hartford search failed: {exc}") from exc
    raise RateLimitError(f"The Hartford search: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of The Hartford's India job postings.

    India is scoped server-side via the `locationCountry` facet (verified
    reliable -- zero non-India leakage across the full 33-job pool).
    `keyword` genuinely narrows results server-side via `searchText`.
    """
    body = {
        "appliedFacets": {"locationCountry": [_INDIA_WID]},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    r = _post_with_retries(body, timeout)

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"The Hartford search returned non-JSON body: {exc}") from exc

    postings = payload.get("jobPostings", [])

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
        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets else ""
        if not job_id:
            continue

        title = (p.get("title") or "").strip()
        if not title:
            continue

        loc = (p.get("locationsText") or "").strip()
        # Every genuine India posting already says "India ..." literally
        # (single-office GCC, see module docstring); appended defensively
        # in case a future posting omits it, same safety net as every
        # sibling Workday fetcher in this repo.
        if "india" not in loc.lower():
            loc = f"{loc}, India" if loc else "India"

        external_path = p.get("externalPath", "")
        if not external_path:
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": f"{_JOB_BASE}{external_path}",
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date via the Workday CXS JSON detail API."""
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/Careers_External/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    last_exc: Exception | None = None
    r = None
    for attempt in range(2):
        try:
            r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"The Hartford description: 429 rate-limited for {api_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"The Hartford description fetch failed: {exc}") from exc

    try:
        info = r.json().get("jobPostingInfo", {})
    except ValueError:
        return "", ""

    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())
    posting_date = info.get("startDate", "") or ""
    return description, posting_date
