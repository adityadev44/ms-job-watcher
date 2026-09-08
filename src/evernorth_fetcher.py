"""Evernorth job fetcher — Workday public REST API (Cigna Group tenant).

Evernorth Health Services is Cigna's pharmacy/care/benefits arm. Its
careers page (``jobs.cigna.com/evernorth``, 301-redirects to
``jobs.thecignagroup.com/evernorth``) turned out to be a Phenom People
front-end/CMS layer — a landing page whose embedded ``phApp.ddo`` JSON is
only a category-count summary, not a real job search response. Reading
that page's own ``applyUrl`` links revealed the true ATS underneath:
plain Workday, tenant ``cigna``, site ``cignacareers``
(``cigna.wd5.myworkdayjobs.com/cignacareers``) — confirmed live via the
standard Workday CXS REST endpoint, same shape as every other Workday
tenant in this repo (Wells Fargo/Citi/Fidelity International/Alegeus/
etc.). Evernorth does not have its own separate Workday tenant; it is one
brand posted through Cigna's shared tenant.

Evernorth's India presence is the "HIH" (Hyderabad Innovation Hub),
opened 2024 — confirmed via web research and live job data (every current
India requisition's title or description names "HIH" and/or "Evernorth"
explicitly; no bare "Cigna Healthcare" India postings were observed).

Verified via direct A/B requests against the live API (2026-09-08):
- Standard nested ``Location_Country`` facet, same cross-tenant India WID
  used elsewhere in this repo (``c4f78be1a8f14da0ab49ce1162348a5e``):
  136 India jobs across the whole Cigna Group tenant.
- ``searchText="Evernorth"`` genuinely narrows that pool server-side to 66
  results (same 66 for ``searchText="HIH"``, confirming these are the same
  Hyderabad-hub postings under two labels) — all 66 are Hyderabad, India.
- **Pagination wraps around** once ``offset`` reaches the true total
  (confirmed live: offset=80 on the 66-job Evernorth-in-India pool returns
  page 1 again, not an empty list — same wraparound family as UBS/Nvidia/
  Walmart/Sprinklr elsewhere in this repo). The API's own ``total`` field
  is also unreliable mid-walk (reads 0 on some interior pages, 66 on
  others for the identical query) — a Verisk/Wells-Fargo-style quirk, not
  trustworthy as a stopping signal.
- Because keyword is pinned to "Evernorth" to scope to this one brand
  within the shared Cigna tenant (mixing that fixed term with a second,
  caller-supplied keyword risks AND-narrowing real jobs out — confirmed
  live: adding "software engineer" on top of "Evernorth" drops the pool
  from 66 to 46), and because the whole pool is small, this fetcher
  ignores the caller's keyword entirely and caches the complete 66-job
  Evernorth-in-India pool on first use (walking pages by fixed offset
  and stopping on the first page whose job IDs are already all seen,
  which also survives the wraparound bug above). Registered in
  ``_IGNORES_KEYWORDS``.

Job descriptions are NOT inline in the search response — fetched from the
Workday CXS JSON detail API, which also returns a real ISO ``startDate``
used as the posting-date proxy (list-level ``postedOn`` is only a
relative string, e.g. "Posted 28 Days Ago").

Live sample titles confirmed real India engineering roles, all Hyderabad:
"Software Engineering Advisor - HIH - Evernorth", "Software Engineering
Manager - HIH - Evernorth", "Data Engineering Senior Manager - HIH -
Evernorth", "Data Science Advisor - HIH - Evernorth".
"""

from __future__ import annotations

import time
import warnings

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://cigna.wd5.myworkdayjobs.com"
_TENANT_PATH = "cignacareers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/cigna/{_TENANT_PATH}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_TENANT_PATH}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/cigna/{_TENANT_PATH}"
_PAGE_SIZE = 20

# India Location_Country WID — the same stable cross-tenant GUID used by
# several other Workday tenants already in this repo.
_INDIA_WID = "c4f78be1a8f14da0ab49ce1162348a5e"

# Fixed search term scoping the shared Cigna Group tenant down to the
# Evernorth / Hyderabad Innovation Hub brand — see module docstring for why
# this is not combined with the caller's own keyword.
_EVERNORTH_KEYWORD = "Evernorth"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/{_TENANT_PATH}",
}

# Module-level cache: the full Evernorth-in-India pool is walked and cached
# once per process (searchText is pinned, and raw pagination wraps around
# past the true total — see module docstring).
_job_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


def _fetch_page(offset: int, timeout: int) -> list[dict]:
    body = {
        "appliedFacets": {"Location_Country": [_INDIA_WID]},
        "limit": _PAGE_SIZE,
        "offset": offset,
        "searchText": _EVERNORTH_KEYWORD,
    }
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout, verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Evernorth Workday: 429 rate-limited")
            r.raise_for_status()
            return r.json().get("jobPostings", [])
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Evernorth fetch failed: {exc}") from exc
    return []


def _fill_cache(timeout: int = 20) -> None:
    """Walk the Evernorth-in-India pool once and cache it.

    _cache_filled is set to True before the walk so a failure doesn't
    trigger a retry storm on every subsequent call in this process
    (Honeywell lesson — see PLAYBOOK "Key Bugs"). Stops on the first page
    that adds no new job ID, which handles both a genuinely short final
    page and the confirmed offset-wraparound bug.
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    seen_ids: set[str] = set()
    collected: list[dict] = []
    offset = 0
    for _ in range(20):  # hard cap: 20 pages * 20 = 400, far above the real ~66-job pool
        page = _fetch_page(offset, timeout)
        if not page:
            break

        new_this_page = 0
        for p in page:
            job_id = (p.get("bulletFields") or [""])[0].strip()
            title = p.get("title", "").strip()
            external_path = p.get("externalPath", "")
            if not (job_id and title and external_path):
                continue
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            new_this_page += 1

            loc = p.get("locationsText", "").strip()
            if "india" not in loc.lower():
                loc = f"{loc}, India" if loc else "India"

            collected.append({
                "id": job_id,
                "title": title,
                "location": loc,
                "posting_date": "",  # resolved via detail API in fetch_job_description
                "application_url": f"{_JOB_BASE}{external_path}",
            })

        if new_this_page == 0:
            break
        offset += _PAGE_SIZE

    _job_cache = collected
    print(f"[Evernorth] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Evernorth-in-India jobs from the cached full pool.

    keyword/location are accepted for interface compatibility but ignored
    — see module docstring for why the search term is pinned to
    "Evernorth" rather than driven by the caller.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_TENANT_PATH}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout, verify=False)
            if r.status_code == 429:
                raise RateLimitError("Evernorth description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"Evernorth description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
