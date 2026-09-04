"""Fetches Deltek job listings via the IBM BrassRing TalentGateway REST API.

Deltek's ATS is IBM BrassRing (Kenexa), hosted at sjobs.brassring.com — the
same platform/API shape as UBS (see ``ubs_fetcher.py``), a different tenant
(partnerid=25397, siteid=5259). Confirmed live via DevTools/curl on
2026-09-04 — the public careers.deltek.com WordPress site (a "Findly"/CWS
plugin skin backed by a Google Cloud Talent Solution search widget) is only
a marketing front end; its own "View All Jobs" link points straight at the
real BrassRing portal:

    https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25397&siteid=5259

Search API (same endpoint shape as UBS):

    POST https://sjobs.brassring.com/TgNewUI/Search/Ajax/PowerSearchJobs

Each request requires:
  - A fresh CSRF token ("RFT" header) obtained from the search page HTML
  - Session cookies set by the same GET request

Unlike UBS, Deltek's tenant is small enough (85 total jobs company-wide,
~34 in India) that keyword search isn't needed for coverage. More usefully,
the server-side "formtext1" ("Work Location") facet genuinely filters —
confirmed via a direct A/B test: an unfiltered search returns 85 jobs
(50/page, and PageNumber does NOT advance past page 1 — same wraparound
bug documented for UBS/MUFG/Nvidia/Walmart), but filtering FacetFilterFields
on the India-flavoured formtext1 facet options returns exactly the true
India count (34) in one page, with JobsCount matching len(Jobs) exactly.
This makes the India facet the fetch strategy: discover the current India
option values from a first, unfiltered call's Facets block (so future new
India cities are picked up automatically, not hardcoded), then re-query with
those options selected.

Locations are always genuinely city/country-shaped ("India-Bangalore",
"India-Bangalore (Remote)", "India-Bangalore (Hybrid)", "India (Remote)") —
no client-side ", India" append needed. All of Deltek's current India
presence is Bangalore; none is in an excluded city.

The response embeds the full job description inline (the "formtext5"
Question field, rich HTML), so no separate description-fetch HTTP call is
needed in the common case — same inline-description shape as UBS. Results
are cached in-module after the first call so every keyword iteration is
free (keyword is accepted for interface compatibility but ignored — see
_IGNORES_KEYWORDS in company_registry.py).

Key fields per job (inside the Questions array):
  reqid        → unique job ID
  jobtitle     → job title
  formtext1    → location ("Work Location" facet)
  formtext5    → HTML job description (inline — no detail-fetch needed)
  lastupdated  → date in "DD-Mon-YYYY" format (e.g. "16-Mar-2026")

The Link field on each job object contains the full application URL.

Note: titles abbreviate "Principal" as "Pncpl" (e.g. "Pncpl Software
Engineer") — same abbreviation pattern already seen at Honeywell ("Engr")
and Disney ("Mgr"). Because matching.exclude_terms's "principal" is
word-boundary matched against the literal word "principal", "Pncpl ..."
titles are NOT excluded by it — this is likely harmless/desirable (Principal
Engineer is usually still an IC role), but recorded here in case a future
exclude_terms review encounters it and wonders why "Pncpl" roles slip
through where a literal "Principal ..." title would not.
"""

from __future__ import annotations

import html as html_mod
import re
import time
import warnings
from datetime import datetime

import requests

_PORTAL_URL = (
    "https://sjobs.brassring.com/TGnewUI/Search/Home/Home"
    "?partnerid=25397&siteid=5259"
)
_SEARCH_URL = "https://sjobs.brassring.com/TgNewUI/Search/Ajax/PowerSearchJobs"
_PAGE_SIZE = 50  # BrassRing default; server-enforced

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Module-level cache — filled once per process run.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}  # url → (description, posting_date)
_cache_filled = False


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent failure from the BrassRing API."""


def _strip_html(raw: str) -> str:
    """Strip HTML tags and decode entities, returning normalised plain text."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(date_str: str) -> str:
    """Convert BrassRing date format 'DD-Mon-YYYY' to 'YYYY-MM-DD'."""
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _get_session_and_token() -> tuple[requests.Session, str]:
    """Create a requests session, load the BrassRing portal page, and extract
    the CSRF token (RFT) required for POST requests."""
    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = session.get(_PORTAL_URL, timeout=20, verify=False)
    r.raise_for_status()

    match = re.search(
        r'name="__RequestVerificationToken"[^>]+value="([^"]+)"', r.text
    )
    if not match:
        match = re.search(
            r'value="([^"]+)"[^>]+name="__RequestVerificationToken"', r.text
        )
    token = match.group(1) if match else ""
    return session, token


def _search(
    session: requests.Session,
    csrf_token: str,
    *,
    facet_options: list[str] | None = None,
    page: int = 1,
    timeout: int = 20,
) -> dict:
    """POST one search page and return the raw parsed JSON response."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": _PORTAL_URL,
        "Origin": "https://sjobs.brassring.com",
        "RFT": csrf_token,
    }

    facet_filter: dict = {"Facet": []}
    if facet_options:
        facet_filter = {
            "Facet": [
                {
                    "Name": "formtext1",
                    "Options": [
                        {"OptionValue": opt, "Selected": True} for opt in facet_options
                    ],
                }
            ]
        }

    body = {
        "PartnerId": "25397",
        "SiteId": "5259",
        "Keyword": [""],
        "ListKeyword": [""],
        "Location": [""],
        "KeywordCustomSolrFields": None,
        "LocationCustomSolrFields": None,
        "Latitude": 0,
        "Longitude": 0,
        "Radius": 0,
        "FacetFilterFields": facet_filter,
        "SortType": "",
        "PageNumber": page,
        "CallType": "SearchButtontype",
        "SocialReferalType": "",
        "PowerSearchOptions": {"PowerSearchOption": []},
        "EncryptedSessionValue": "",
        "localizedStrings": {},
        "JobSiteIds": "",
        "RunSavedSearch": False,
        "TurnOffHttps": False,
        "LinkID": 0,
        "JobCountOnly": False,
        "SearchResumeName": "",
        "MatchedReqIds": [],
        "ClearSession": False,
        "UserGivenKeyWords": "",
        "BringAllJobs": False,
        "RequestForMap": False,
    }

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = session.post(
            _SEARCH_URL,
            headers=headers,
            json=body,
            timeout=timeout,
            verify=False,
        )

    if r.status_code == 429:
        raise RateLimitError("Deltek BrassRing: 429 rate-limited")
    r.raise_for_status()
    return r.json()


def _discover_india_facet_options(
    session: requests.Session, csrf_token: str, timeout: int = 20
) -> list[str]:
    """Run one unfiltered search and pull the India-flavoured Work Location
    facet option values out of the response's own Facets block, so newly
    opened India cities are picked up automatically rather than hardcoded."""
    data = _search(session, csrf_token, facet_options=None, page=1, timeout=timeout)
    for facet in data.get("Facets", {}).get("Facet", []) or []:
        if facet.get("Name") == "formtext1":
            return [
                opt["OptionValue"]
                for opt in facet.get("Options", [])
                if "india" in str(opt.get("OptionName", "")).lower()
            ]
    return []


def _fill_cache(timeout: int = 20) -> None:
    """Populate _cache with all unique India Deltek jobs.

    Descriptions are embedded inline in the BrassRing search results, so this
    also fills _desc_cache keyed by application_url.
    """
    for attempt in range(3):
        try:
            session, csrf_token = _get_session_and_token()
            break
        except Exception as exc:
            if attempt == 2:
                raise RateLimitError(
                    f"Deltek: could not load portal page: {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    try:
        india_options = _discover_india_facet_options(session, csrf_token, timeout)
    except RateLimitError:
        raise
    except Exception as exc:
        raise RateLimitError(f"Deltek: facet discovery failed: {exc}") from exc

    if not india_options:
        # Safety net: if the facet ever disappears/renames, fall back to an
        # unfiltered fetch and rely purely on the client-side "india" check
        # below rather than returning nothing.
        india_options = None

    seen_ids: set[str] = set()
    first_id_seen: str | None = None

    for page in range(1, 4):  # guard against pagination wraparound
        try:
            data = _search(
                session, csrf_token, facet_options=india_options, page=page, timeout=timeout
            )
        except RateLimitError:
            raise
        except Exception as exc:
            raise RateLimitError(f"Deltek search failed (page={page}): {exc}") from exc

        raw_jobs = data.get("Jobs", {}).get("Job", []) or []
        if not raw_jobs:
            break

        page_first_id = str(
            {q["QuestionName"]: q["Value"] for q in raw_jobs[0].get("Questions", [])}.get(
                "reqid", ""
            )
        )
        if page_first_id and page_first_id == first_id_seen:
            break  # pagination wrapped back to page 1 — stop
        if first_id_seen is None:
            first_id_seen = page_first_id

        new_this_page = 0
        for job in raw_jobs:
            qs = {q["QuestionName"]: q["Value"] for q in job.get("Questions", [])}
            job_id = str(qs.get("reqid", "")).strip()
            if not job_id or job_id in seen_ids:
                continue

            loc = " ".join(str(qs.get("formtext1", "")).strip().split())
            if "india" not in loc.lower():
                continue  # client-side India safety net

            seen_ids.add(job_id)
            new_this_page += 1

            # BrassRing exposes two title fields: "jobtitle" (abbreviated
            # internal system title, e.g. "Pncpl Software Engineer") and
            # "formtext4" (unabbreviated candidate-facing posting title,
            # e.g. "AI Solutions Engineer"). formtext4 looks friendlier at
            # first glance, but it's a real trap here: deliberately tested
            # both, and formtext4 changes some titles just enough to collide
            # with matching.exclude_terms's "solutions engineer" phrase
            # (intended for pre-sales/customer-facing roles) on a genuine
            # hands-on AI/LLM engineering posting (job 623208, "AI Solutions
            # Engineer") — silently dropping a real match that jobtitle's
            # "Pncpl Software Engineer" does not trip. Getting the alert
            # with a slightly cryptic title beats a prettier title with no
            # alert at all, so this stays on jobtitle. See PLAYBOOK.md Key
            # Bugs for the full writeup.
            title = str(qs.get("jobtitle", "")).strip()
            raw_date = str(qs.get("lastupdated", "")).strip()
            posting_date = _parse_date(raw_date)
            app_url = job.get("Link", "")
            desc_html = str(qs.get("formtext5", "")).strip()
            description = _strip_html(desc_html)

            _cache.append({
                "id": job_id,
                "title": title,
                "location": loc,
                "posting_date": posting_date,
                "application_url": app_url,
            })
            _desc_cache[app_url] = (description, posting_date)

        if new_this_page == 0:
            break

        total = data.get("JobsCount", 0) or 0
        if total and len(seen_ids) >= total:
            break

        time.sleep(0.3)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of the cached India Deltek job listings.

    The cache is filled on the first call via the India Work Location facet,
    which covers the entire India pool in one request (Deltek's tenant is
    small: ~34 India jobs). The caller's keyword/location arguments are
    accepted for interface compatibility but not used for filtering.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True
        _fill_cache(timeout=timeout)
    return _cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a job.

    Deltek's BrassRing tenant embeds the full job description in the search
    results, so this simply reads from the in-memory description cache
    populated during fetch_jobs(). No additional HTTP call is needed in the
    common case.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    # Fallback: re-run the cache fill once in case this was called before
    # fetch_jobs() populated it (shouldn't normally happen via run_company.py).
    if not _cache_filled:
        fetch_jobs("", "India", timeout=timeout)
        if application_url in _desc_cache:
            return _desc_cache[application_url]

    return "", ""
