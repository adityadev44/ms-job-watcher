"""Fetches Johnson & Johnson job listings via the Workday public REST API.

J&J's ATS is Workday, hosted at jj.wd5.myworkdayjobs.com (tenant "jj", site
"JJ" -- confirmed live 2026-09-13 via a direct POST to
/wday/cxs/jj/JJ/jobs, which returned real jobPostings (HTTP 200,
total=1766 globally). The tenant/site pair was found directly (no guessing
needed -- a plain web search for "myworkdayjobs.com" + J&J turned up the
exact `jj.wd5.myworkdayjobs.com/JJ/...` URL).

Unlike Novartis/Pfizer/Wells Fargo (all of which expose a nested
`locationCountry` facet inside `locationMainGroup` that accepts a single
India WID), this tenant's only nested facet under `locationMainGroup` is a
flat `locations` list of ~520 individual city/state/region values with NO
country grouping at all -- the same shape already documented for Genpact
("no usable location facet") except this tenant DOES expose usable
per-city WIDs, just not a single country-level one. Confirmed by fetching
the full facet list and finding 24 raw entries containing "india"
case-insensitively, of which 6 are false positives that must be excluded
(Fort Wayne/Mooresville/Valparaiso/Warsaw, Indiana US cities; "Indiana (Any
City)"; "Indianapolis, Indiana, United States") -- leaving 17 genuine
India city/state WIDs (Ahmedabad, Aurangabad, Bangalore, Chandigarh,
Chennai, Delhi, Gurgaon, Hyderabad, Jaipur, Kanpur, Kolkata, Ludhiana,
Mumbai [2 distinct WIDs -- "Mumbai, India" and "Mumbai, Maharashtra,
India" both exist as separate facet values], Patna, Penjerla, Pune,
Raipur). Applying all 17 as `appliedFacets.locations` narrowed the global
1766-job pool to a genuine, verified India-only 94-job pool (spot-checked
every returned `locationsText` value -- no non-India location leaked
through, unlike the Micron/Verizon/Lowe's broken-facet lesson).

Confirmed live 2026-09-13: of the 94 India postings, a `searchText`
keyword pass genuinely narrows server-side (total=19 for "software
engineer" vs 94 unfiltered, total=3 for "generative ai") -- keywords are
NOT ignored here, so this fetcher is not added to `_IGNORES_KEYWORDS`.
Real SDE-relevant titles exist in the India pool today: "Sr. Engg, Forward
Deployed Engineer, Platform Engineering", "Eng RD Soft Eng", "Sr Eng RD
Soft Eng", "Product Engineer", "Mgr, Forward Deployed Engineer, Data &
Intelligence" (a real generative-ai-flavored hit), almost entirely
Hyderabad-based -- a genuine software/platform engineering GCC presence,
not just commercial/sales/finance roles (which dominate the rest of the
94-job pool).

`locationsText` on this tenant already includes the literal ", India"
suffix for every genuine India posting (e.g. "Hyderabad, Andhra Pradesh,
India") -- confirmed directly, so no client-side append is needed here
(unlike Novartis/Invesco). A defensive word-boundary re-check is still
applied before accepting a location as India-flavored, guarding against
the same "Indianapolis"/"Indiana" false-positive class documented for
PayPal/FactSet, even though the pre-filtered WID list should already
exclude those.

Two Workday quirks confirmed live on this tenant, both guarded here even
though live testing did not surface either as an active bug (cheap
insurance, same discipline as every other Workday fetcher in this repo):
- Page size is capped at 20 -- limit=25 returns a raw HTTP 400 (same class
  as Northern Trust/Pfizer/Novartis's cap).
- Pagination was tested through the real last page (offset=80 of 94, then
  offset=100/120) and correctly returned an empty `jobPostings` array with
  no wraparound -- but the page1-first-ID memo guard is still applied
  defensively, matching every other Workday fetcher in this repo, in case
  a different keyword's result set exhibits the wraparound bug that this
  smoke test's exact query didn't hit.

Job detail descriptions come from the same Workday CXS JSON detail API
shape as Novartis/Pfizer: GET .../wday/cxs/jj/JJ{externalPath} returns a
`jobPostingInfo.jobDescription` HTML blob and an already-ISO
`jobPostingInfo.startDate` (confirmed live: "2026-09-11", no relative-date
parsing needed for this field).
"""

from __future__ import annotations

import re
import time
import warnings
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jj.wd5.myworkdayjobs.com"
_SEARCH_URL = f"{_BASE_URL}/wday/cxs/jj/JJ/jobs"
_JOB_BASE = f"{_BASE_URL}/JJ"
_DETAIL_BASE = f"{_BASE_URL}/wday/cxs/jj/JJ"

_PAGE_SIZE = 20
_MAX_LIMIT = 20

# Genuine India city/state facet WIDs under the flat `locations` facet --
# this tenant has no country-level facet to apply instead. Excludes the
# "Indiana, United States"/"Indianapolis" false-positive entries that also
# matched a naive "india" substring search over the raw facet list.
_INDIA_LOCATION_WIDS = [
    "a453930323781001ccb3f86614f60000",  # Ahmedabad, Gujarat, India
    "248d949319841001cb07b39577950000",  # Aurangabad, Maharashtra, India
    "396b78684e421001cb04df00caaf0000",  # Bangalore, Karnataka, India
    "385f2db984b61001ccc07c5e77f10000",  # Chandigarh, Chandigarh, India
    "83c5c57cda9d1001cb05f96847b80000",  # Chennai, Tamil Nadu, India
    "385f2db984b61001ccc10ea5cbb20000",  # Delhi, Delhi, India
    "3f2a08fac1611001cb181c39ac4c0000",  # Gurgaon, Haryana, India
    "9ab785410a571001ccc0c42979e70000",  # Hyderabad, Andhra Pradesh, India
    "40baa0b21fa41001ccb9d496368a0000",  # Jaipur, Rajasthan, India
    "b7ec5694ba561001ccb7b4bc503d0000",  # Kanpur, Uttar Pradesh, India
    "22c0964533ff1001cb0f399634750000",  # Kolkata, West Bengal, India
    "396b78684e421001ccb8326a7aa00000",  # Ludhiana, Punjab, India
    "a453930323781001cb123acf67210000",  # Mumbai, India
    "4b4e8ca78d381001cb0fcc4d038b0000",  # Mumbai, Maharashtra, India
    "b7ec5694ba561001ccb95df72ab30000",  # Patna, Bihar, India
    "4a6c44a463ba1001cb13010f78530000",  # Penjerla, Telangana, India
    "ab23c390dc351001cb0f8834ee5a0000",  # Pune, Maharashtra, India
    "a453930323781001ccb90017a5420000",  # Raipur, Chhattisgarh, India
]

_INDIA_WORD_RE = re.compile(r"\bindia\b", re.IGNORECASE)

_page1_first_id: dict[str, str] = {}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": f"{_BASE_URL}/JJ",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Workday."""


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


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    body = {
        "appliedFacets": {"locations": _INDIA_LOCATION_WIDS},
        "limit": min(num, _MAX_LIMIT),
        "offset": start,
        "searchText": keyword,
    }

    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.post(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    json=body,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("J&J Workday: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"J&J fetch failed: {exc}") from exc

    postings = r.json().get("jobPostings", [])

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
        loc = p.get("locationsText", "").strip()
        if not loc or not _INDIA_WORD_RE.search(loc):
            # Pre-filtered via India-only WIDs -- a location string that
            # doesn't actually say India (or a stray "Indiana"/"Indianapolis"
            # match) should never happen, but skip defensively rather than
            # mislabel it.
            continue

        title = p.get("title", "").strip()
        if not title:
            continue

        external_path = p.get("externalPath", "")

        bullets = p.get("bulletFields", [])
        job_id = bullets[0].strip() if bullets and bullets[0].strip() else external_path
        if not job_id:
            continue

        app_url = f"{_JOB_BASE}{external_path}" if external_path else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(p.get("postedOn", "")),
            "application_url": app_url,
        })

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description via the Workday CXS JSON detail API.

    Returns (description_text, posting_date).
    The startDate field in the detail response is already ISO (YYYY-MM-DD).
    """
    if _JOB_BASE in application_url:
        ext_path = application_url[len(_JOB_BASE):]
    else:
        ext_path = "/" + application_url.split("/JJ/", 1)[-1]
    api_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(
                    api_url,
                    headers=_HEADERS,
                    timeout=timeout,
                    verify=False,
                )
            if r.status_code == 429:
                raise RateLimitError("J&J description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RateLimitError(f"J&J description fetch failed: {exc}") from exc

    info = r.json().get("jobPostingInfo", {})
    raw_html = info.get("jobDescription", "") or ""
    description = " ".join(BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split())

    posting_date = info.get("startDate", "") or ""

    return description, posting_date
