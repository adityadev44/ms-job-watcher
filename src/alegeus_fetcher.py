"""
Alegeus job fetcher — Workday public REST API (alegeus.wd1.myworkdayjobs.com).

Alegeus (US healthcare-benefits/HSA-FSA-COBRA administration software
product company) was flagged in research as a possible target for recurring
.NET demand, with a caveat to check whether it had already been absorbed
into Optum's hiring pipeline after Optum Financial's July 2026 acquisition
(closed 2026-07-02, ~$3B, per Optum's own newsroom and multiple trade-press
reports). **Live-verified 2026-09-04: it has NOT been absorbed.** Alegeus
still runs its own distinct Workday tenant
(``alegeus.wd1.myworkdayjobs.com/Alegeus_External_Careers``), and every job
detail response's ``hiringOrganization.name`` reads "Alegeus Technologies
India Private Limited" — no Optum/UnitedHealth branding anywhere in the
tenant, career-site copy, or hiring-entity name. Standard REST Workday CXS
API, same pattern as Wells Fargo/Citi/Fidelity/etc. — plain ``requests``,
no Playwright needed.

Verified via direct A/B requests against the live API (not assumed):
- No flat ``locationCountry``/``Location_Country`` facet exists on this
  small tenant (unlike most Workday tenants in this repo) — the only
  location facet is ``locations`` (nested under ``locationMainGroup``),
  whose "Bangalore - India" value (WID
  ``14a6034b937b0101fc47cc1e4cf50000``) is applied directly. All of
  Alegeus's India jobs are in this one city; ``locationsText`` always reads
  "Bangalore - India" (contains "India" natively — no client-side append
  needed).
- ``searchText`` genuinely filters server-side: 13/13 India results for an
  empty query, 0 for a nonsense token, 10 for ``.NET`` — confirmed via
  direct A/B requests, so this fetcher is NOT registered in
  ``_IGNORES_KEYWORDS``.
- ``offset`` terminates cleanly past the true total (0 results at
  offset=20 against a 13-job pool) — no UBS/MUFG/Nvidia-style wraparound
  observed on this tenant.
- Company-wide pool is tiny (25 total jobs, 13 in India) — no pagination
  edge cases in practice; still paginate defensively via ``limit``/``offset``
  like every other Workday fetcher here.

Job descriptions are NOT inline in the search response (Wells Fargo
pattern, not the HealthEdge/PepsiCo inline-description pattern) — fetched
from the Workday CXS JSON detail API at
``GET /wday/cxs/alegeus/Alegeus_External_Careers{externalPath}``, which
returns a real ISO ``startDate`` used as the posting-date proxy (list-level
``postedOn`` is only a relative string, e.g. "Posted 30+ Days Ago").

Real .NET/C# matches confirmed live: multiple "(Expert) Software Engineer"
titles explicitly name ASP.NET/C#, "C#/.NET microservices on Azure", and
"C#/.NET Core (3.1/5+)" in the JD body — this is a genuine, product-company
.NET shop, consistent with the research note flagging Alegeus for recurring
.NET demand. One real AI/ML/Python-track job also exists ("Sr. Engineer II,
ML Ops", R-101285 — explicitly names LangChain/CrewAI/AutoGen/vector
databases/RAG architectures) but its title does not match any
``title_family`` phrase ("Sr." is never expanded to "senior" by
matcher.py's ``_normalize_text``, so "Sr. Engineer II, ML Ops" doesn't hit
"senior engineer") — a title-family precision gap, same class already
flagged for CitiusTech/DAZN/Novartis in the playbook, not fixed here. See
the onboarding report for exact job IDs.
"""
from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://alegeus.wd1.myworkdayjobs.com"
_TENANT_PATH = "Alegeus_External_Careers"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/alegeus/{_TENANT_PATH}/jobs"
_JOB_BASE = f"{_BASE_URL}/{_TENANT_PATH}"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/alegeus/{_TENANT_PATH}"
_PAGE_SIZE = 20

# "Bangalore - India" value under the `locations` facet (nested under
# `locationMainGroup`) — this small tenant has no flat locationCountry
# facet at all. Verified 2026-09-04: 13/13 current India jobs, all
# Bangalore. Stable WID (same shape as every other Workday tenant's facet
# IDs in this repo).
_INDIA_LOCATION_WID = "14a6034b937b0101fc47cc1e4cf50000"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_JOB_BASE}",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


# ---------------------------------------------------------------------------
# Date helper — Workday returns relative strings like "Posted 3 Days Ago"
# at the list level (the detail API has a real ISO startDate instead).
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


def _extract_job_id(external_path: str, bullet_fields: list) -> str:
    """Prefer the R-###### bullet field; fall back to parsing externalPath."""
    for field in bullet_fields or []:
        m = re.match(r"^(R-\d+)$", str(field).strip(), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    m = re.search(r"_(R-\d+)$", external_path or "", re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return ""


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
    """Return a page of Alegeus's India job postings.

    India is scoped server-side via the `locations` facet's "Bangalore -
    India" WID (this tenant has no flat locationCountry facet). `keyword`
    genuinely narrows results server-side (verified via A/B request); the
    `location` parameter is not sent — India scoping is always applied via
    the hardcoded facet, same reasoning as every other single-facet Workday
    fetcher in this repo.
    """
    body = {
        "appliedFacets": {"locations": [_INDIA_LOCATION_WID]},
        "limit": num,
        "offset": start,
        "searchText": keyword,
    }

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Alegeus Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Alegeus search failed: {exc}") from exc

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Alegeus search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for p in payload.get("jobPostings", []):
        external_path = p.get("externalPath", "")
        job_id = _extract_job_id(external_path, p.get("bulletFields", []))
        if not job_id:
            continue

        title = (p.get("title") or "").strip()
        if not title:
            continue

        loc = (p.get("locationsText") or "").strip()
        # Safety net: skip anything that doesn't genuinely say India, in
        # case a future non-Bangalore India location or a facet change
        # ever lets a non-India job through.
        if "india" not in loc.lower():
            continue

        application_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": application_url,
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date via the Workday CXS JSON detail API.

    Transforms the public job-page URL into the equivalent JSON API URL
    (same shape as wellsfargo_fetcher.py).
    """
    if application_url.startswith(_JOB_BASE):
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split(f"/{_TENANT_PATH}/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    r = None
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(api_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Alegeus description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            raise RateLimitError(f"Alegeus description fetch failed: {exc}") from exc

    try:
        info = r.json().get("jobPostingInfo", {})
    except ValueError:
        return "", ""

    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )
    # startDate is already YYYY-MM-DD from the API.
    posting_date = info.get("startDate", "") or ""

    return description, posting_date
