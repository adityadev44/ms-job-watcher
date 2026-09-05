"""
Omnissa job fetcher — Workday public REST API (CXS).

Omnissa (`omnissa.com`) is the 2023 spinoff of Broadcom/VMware's End-User
Computing division (Workspace ONE, Horizon), later acquired by KKR — a
genuinely new, independent company rather than a shared VMware/Broadcom
tenant, exactly as flagged in the onboarding brief. Verified live
(2026-09-05) via the branded careers page (`www.omnissa.com/careers/jobs/`):
its own job-search results embed real `myworkdayjobs.com` URLs for both the
job-detail page and the "Apply" link
(`omnissa.wd501.myworkdayjobs.com/Omnissa_External_Career_Site/job/...`),
confirming Workday is the actual backing ATS — a fresh tenant of its own
(`omnissa`, site `Omnissa_External_Career_Site`), not inherited from VMware.
No skin/aggregator layer in front of it (unlike Eurofins/Boeing/GE
Aerospace) — the branded page's own client-side JS calls the CXS API
directly.

Confirmed live via direct POST to the CXS search endpoint
(`https://omnissa.wd501.myworkdayjobs.com/wday/cxs/omnissa/
Omnissa_External_Career_Site/jobs`):
- Whole-company pool is small: 105 total postings globally.
- This tenant DOES expose a usable, reliable location facet
  (`locationMainGroup` -> nested `locations` facetParameter) — unlike
  Genpact/SimCorp, which have no location facet at all. Four facet IDs
  cover every India-labelled entry: "Bengaluru, India" (14), "Chennai,
  India" (4), "India-Bangalore-Office-Kalyani Vista" (2), "Remote - India"
  (1) — applying all four via `appliedFacets.locations` returns exactly 21
  total postings, and every single one already carries a genuine "India"
  city/label in `locationsText` (verified: zero non-India leakage, and zero
  of the returned 21 came back as an ambiguous "N Locations" label).
- Cross-checked the ~30 *other* "N Locations" postings across the full
  105-job pool (SimCorp-style ambiguous multi-site reqs) by resolving each
  one's true country via the CXS detail endpoint
  (`jobPostingInfo.country.descriptor`): every single one resolved to
  "United States of America" — none are hidden India roles. Confirmed the
  facet is trustworthy on its own for this tenant; no per-posting ambiguous-
  location resolution is needed today. A defensive fallback for a future
  ambiguous India-facet-matching posting is still included (same pattern as
  simcorp_fetcher.py) in case that ever changes.
- `searchText` (keyword) genuinely narrows results server-side within the
  India facet (A/B verified: '' -> 21, 'software engineer' -> 18, 'python'
  -> 6, a nonsense token -> 0) — this API is NOT a keyword no-op. However,
  since the whole India-relevant pool is only 21 postings, this fetcher
  caches the full India-faceted pool ONCE per process and ignores `keyword`
  entirely (same rationale as simcorp_fetcher.py/persistent_fetcher.py) —
  simpler and avoids a real quirk found while probing keywords: the OBVIOUS
  literal default keyword ".NET developer" returns 0 India results on this
  tenant (JD text says "C#.net"/"C#, .NET Core", never the two-word phrase
  "developer" right after "NET"), which would have silently under-fetched
  under a per-keyword-pass design. Because the fetcher module itself never
  uses `keyword`, this should be registered in `_IGNORES_KEYWORDS` for
  consistency with how run_company.py collapses the keyword loop for
  cache-once fetchers — the ATS itself is not a keyword no-op, the fetcher
  choosing to ignore it is.
- Descriptions are NOT inline in the search response — fetched from the
  Workday CXS detail endpoint (`GET .../wday/cxs/omnissa/
  Omnissa_External_Career_Site{externalPath}` -> `jobPostingInfo
  .jobDescription`, HTML, stripped here). The detail endpoint's own
  `startDate` field (e.g. "2026-08-27") is used as the authoritative posting
  date when available, since it is already an absolute YYYY-MM-DD-shaped
  date rather than the search response's relative "Posted N Days Ago"
  string.

Real matches confirmed by fetching all 21 India postings' live descriptions
directly (2026-09-05, bypassing seen-state):
- 4 genuine `.NET / C#` primary-skill hits, all real (not company-blurb
  boilerplate — every JD opens with an identical "Omnissa Platform is the
  first AI-driven digital work platform..." paragraph that does NOT itself
  contain any hard skill term, so a hit elsewhere in the body is a real
  requirement, not a false positive from that shared intro): "C# Backend
  Software Developer - Bengaluru" (R-102201, explicit "C#, ASP.NET,
  Angular"), "Senior Software Engineer (C#, Distributed systems)-
  Bengaluru" (R-100949, same stack), "Software Engineer/Senior SE/Staff
  Engineer- C#.net - Bengaluru" (R-101683, same stack), "Software Engineer
  (Platform/Backend) - ClickHouse - Bengaluru" (R-101972, "Strong working
  experience with C#, Go, and Docker").
- No `AI / ML / Python` primary-skill hit passes today: two title_family-
  passing candidates ("Software Engineer (Backend Engineer)" x2, R-101340 /
  R-101758) mention only bare "python" in their JD, which is deliberately a
  broad-only `skills` signal (not sufficient alone per config.yaml's design
  — no bare-Python-only pass) — a genuine current zero for this track, not
  a fetcher defect.
- **New title_family precision-gap instances found, NOT fixed (same "flag,
  don't silently patch" discipline as every other such gap in this
  repo's playbook)**: three real title_family misses cost real matches
  here. "Member Technical Staff 3" (R-101860, Bengaluru) explicitly
  requires "Experience integrating Generative AI, LLMs, AI agents...
  familiarity with RAG architectures, vector databases, prompt
  engineering" — a genuine `AI / ML / Python` primary-skill hit — but
  "member technical staff"/"member of technical staff" matches no
  title_family phrase (same class of gap as DAZN's "Conversational
  Developer"/PepsiCo's "Development Manager", a new title SHAPE this
  time). "Performance Engineer" and "Sr Performance Engineer" (R-102200 /
  R-101723, both Bengaluru) both explicitly require ".NET Framework and
  .NET Core web services" and "hands-on experience in C#, Java, Go,
  Groovy, or Python" — genuine `.NET / C#` primary-skill hits — but
  "performance engineer" is not a title_family phrase either. All three
  are real, currently-open, would-be matches lost purely to this gap;
  left unfixed here per standing repo policy (a dedicated precision pass
  handles the whole backlog, not one company at a time).
"""
from __future__ import annotations

import html as _html_mod
import re
import time
from datetime import date, timedelta

import requests

_BASE_URL = "https://omnissa.wd501.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/omnissa/Omnissa_External_Career_Site/jobs"
_JOB_BASE = f"{_BASE_URL}/Omnissa_External_Career_Site"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/omnissa/Omnissa_External_Career_Site"
_PAGE_SIZE = 20
_MAX_PAGES = 10  # safety ceiling (~200 jobs) — real India-faceted pool is ~21

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_JOB_BASE}",
}

# Workday location-facet IDs confirmed live (2026-09-05) to cover every
# India-labelled posting on this tenant — see module docstring. Combining
# all four in one `appliedFacets.locations` list narrows the ~105-job global
# pool to exactly the ~21 genuine India postings server-side.
_INDIA_LOCATION_FACETS = [
    "fd198f67c9fa1001e718875976140000",  # Bengaluru, India
    "f26f2297fdb11001e4bc88d2b1c20000",  # Chennai, India
    "3003867489261000c81cd2eca56a0000",  # India-Bangalore-Office-Kalyani Vista
    "fd198f67c9fa1001e718fff860df0000",  # Remote - India
]

_desc_cache: dict[str, tuple[str, str]] = {}

# Module-level cache: the India-faceted pool is resolved once per process
# and reused for every keyword/location call this tenant's tiny pool makes
# re-fetching per keyword pointless (same pattern as simcorp_fetcher.py).
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


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


def _job_id_from_posting(p: dict, external_path: str) -> str:
    for field in p.get("bulletFields", []):
        m = re.match(r"^(R-\d+)$", str(field).strip(), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    m = re.search(r"_(R-\d+)(?:-\d+)?$", external_path, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def _post_with_retries(url: str, body: dict, timeout: int, label: str):
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=_HEADERS, json=body, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Omnissa {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Omnissa {label} failed: {exc}") from exc
    raise RateLimitError(f"Omnissa {label}: no response — {last_exc}")


def _get_with_retries(url: str, timeout: int, label: str):
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Omnissa {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Omnissa {label} failed: {exc}") from exc
    raise RateLimitError(f"Omnissa {label}: no response — {last_exc}")


def _resolve_ambiguous_location(external_path: str, timeout: int) -> str | None:
    """Resolve an "N Locations" posting (defensive fallback only — no such
    posting has ever been observed inside the India-faceted result set, see
    module docstring) via its detail page's true country.

    Returns "India" if genuinely India-based, otherwise None (skip rather
    than risk a false alert).
    """
    try:
        r = _get_with_retries(f"{_DETAIL_BASE}{external_path}", timeout, "location resolve")
    except RateLimitError:
        return None
    info = r.json().get("jobPostingInfo", {})
    country = (info.get("country") or {}).get("descriptor", "")
    if country.strip().lower() != "india":
        return None
    return "India"


def _fill_cache(timeout: int = 20) -> None:
    """Paginate the India-faceted result set once and cache every posting.

    _cache_filled is set before the loop so a mid-run failure doesn't cause
    a retry storm on every subsequent keyword call (Honeywell/Persistent
    lesson) — a scan cycle that hits an error here simply yields no Omnissa
    jobs this cycle and self-heals on the next 30-minute run.
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    all_postings: list[dict] = []
    offset = 0
    for page_num in range(_MAX_PAGES):
        if page_num > 0:
            time.sleep(0.15)
        body = {
            "appliedFacets": {"locations": _INDIA_LOCATION_FACETS},
            "limit": _PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        r = _post_with_retries(_SEARCH_URL, body, timeout, "search")
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        all_postings.extend(postings)
        offset += len(postings)
        if offset >= data.get("total", 0):
            break

    seen_job_ids: set[str] = set()
    collected: list[dict] = []
    for p in all_postings:
        external_path = p.get("externalPath", "")
        job_id = _job_id_from_posting(p, external_path)
        title = (p.get("title") or "").strip()
        if not (job_id and title and external_path):
            continue
        if job_id in seen_job_ids:
            continue
        seen_job_ids.add(job_id)

        loc_text = (p.get("locationsText") or "").strip()
        location: str | None
        if "location" in loc_text.lower() and any(ch.isdigit() for ch in loc_text):
            # Ambiguous "N Locations" label — not observed in this facet's
            # result set live, but resolved defensively if it ever appears.
            time.sleep(0.1)
            location = _resolve_ambiguous_location(external_path, timeout)
        elif "india" in loc_text.lower():
            location = loc_text
        else:
            # Facet applied is India-only; a non-"india"-labelled, non-
            # ambiguous result here would be unexpected — skip rather than
            # risk a mislabeled alert.
            location = None

        if not location:
            continue

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": f"{_JOB_BASE}{external_path}",
        })

    _india_cache = collected
    print(f"[Omnissa] Cache filled: {len(collected)} India jobs (of {len(all_postings)} fetched)")


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
) -> list[dict]:
    """Return a page of Omnissa's India job postings.

    `keyword` is ignored: the tenant's own India-faceted pool is tiny (~21
    postings) so the whole set is cached once and sliced here rather than
    re-querying per keyword — see module docstring for why (a real
    server-side keyword filter exists, but caching once is simpler and
    avoids that filter's own quirks, e.g. ".NET developer" returning 0).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date via the Workday CXS detail API."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/Omnissa_External_Career_Site/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = _get_with_retries(api_url, timeout, "description")
    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = _strip_html(raw_html)
    posting_date = info.get("startDate", "") or ""

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
