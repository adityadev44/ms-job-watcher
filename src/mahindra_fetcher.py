"""Fetches Mahindra & Mahindra's own (parent auto/tractor/farm-equipment
conglomerate) job listings via the SAP SuccessFactors J2W (classic)
self-hosted career site (jobs.mahindracareers.com).

NOTE: this is Mahindra & Mahindra's OWN corporate/auto/farm-equipment
careers site -- DISTINCT from ``techmahindra_fetcher.py`` (Tech Mahindra),
which is already onboarded separately with its own ATS.

ATS discovery (live, 2026-09-07): mahindra.com/career links to Mahindra's
own business-unit career pages (auto.mahindra.com, mahindrafinance.com,
etc.) plus a group-wide portal at ``jobs.mahindracareers.com`` (linked
straight from the "Farm Equipment"/"Agri" campus-hiring blurbs on the
corporate careers page). That portal is the exact same SAP SuccessFactors
J2W "classic" ``<tr class="data-row">`` table theme already known from
Birlasoft/Dover/Nomura/Capgemini/SAP Labs/Mastek in this repo (JSESSIONID
cookie, `/platform/js/j2w/` bundle, apply form posting to
`career4.successfactors.com/career?...&company=Mahindra`) -- confirmed
live, plain ``requests`` works with no auth/session dance.

Search endpoint: ``GET /search/?q=<kw>&locationsearch=India&startrow=<n>``

**Critical gotcha, discovered live and NOT present on Birlasoft's tenant**:
a `q` value with zero real matches does NOT render an empty result set --
it silently swaps in "the 25 most recent jobs posted by Mahindra & Mahindra
Limited" instead, under a label containing the literal text 'There are
currently no open positions matching "..."'. This is NOT limited to
obviously-nonsense tokens: it also fires mid-pagination the moment
`startrow` exceeds a REAL keyword's own true result count (confirmed live:
searching "manager" returns genuine, differentiated results through
startrow=500, then flips to the same generic 25-job fallback list at
startrow=1000; searching "angular"/"dot net", each with only 1-2 true
matches, already flips to the fallback at startrow=10). A naive "stop when
rows are empty" pagination guard would NOT catch this -- the fallback page
still has 25 real `<tr class="data-row">` rows, just for jobs that don't
match the query at all. This fetcher therefore checks for that literal
label text on every page (not just page 0) and treats its presence as "zero
real results from here on", exactly as if the row list were empty --
this is the load-bearing correctness fix for this tenant.

A second, related quirk: `q=""` (or omitting `q` entirely) does NOT reach
this same search grid at all -- it renders a different, non-paginating
"10 recent jobs" homepage-style widget instead (`startrow` has no effect
on it). Real keyword-bearing searches are therefore required to reach the
genuine paginated grid; this fetcher never sends `q=""`.

Server-side filtering — tested live against the real API with the standard
keyword list: counts differ per keyword and the matching is loose/OR-based
across tokens (e.g. "senior software engineer"/".NET developer"/"generative
ai engineer" each still return real, non-fallback results, same
over-inclusive-not-under-inclusive behavior as HDFC Bank/RippleHire
elsewhere in this repo) -- safe to search per-keyword rather than caching
the whole (much larger, ~1000+ job) board once. ``locationsearch=India``
was tested combined with several narrow real keywords (e.g. "angular"=1
result, "dot net"=2) and never zeroed a genuine result (unlike Air India's
tenant), so it is sent on every call.

Page size is a fixed 10/page (confirmed live via overlapping-window
comparison at startrow=0/5/10 -- NOT 25 like Birlasoft's tenant; the "25"
figure that does appear is only ever the unrelated fallback list's size).

Location: every observed posting's location string ends in ", IN" (e.g.
"Chennai, MUM-KND-AFS(AD), IN") -- same pattern as Dover's tenant elsewhere
in this repo, never the literal word "India" -- normalized here the same
way Birlasoft/Dover handle it (", IN" -> ", India").

Detail pages: no posting date is available in the search grid at all (the
row only carries title/location/facility/sector/department columns) -- a
separate detail-page fetch is required for `posting_date`, same as HDFC
Bank. Detail pages use the exact same selectors as Birlasoft's tenant:
`<span class="jobdescription">` for the full JD and `<meta
itemprop="datePosted" content="Thu Aug 13 00:00:00 UTC 2026">` for the
posting date -- confirmed live on a real "Manager - 3D Data Engineer,
Accessories" posting.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.mahindracareers.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 10  # confirmed live; NOT the 25/page some sibling J2W tenants use
_NO_MATCH_LABEL = 'no open positions matching'

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Per-keyword pagination-wraparound guard (see repo contract) -- kept as a
# defensive backstop in addition to the fallback-page detection above,
# which is this tenant's real, load-bearing overshoot guard.
_FIRST_PAGE_IDS: dict[str, set[str]] = {}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Mahindra's J2W site."""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Thu Aug 13 00:00:00 UTC 2026' (meta tag) -> '2026-08-13'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalise_location(raw: str) -> str:
    """'Chennai, MUM-KND-AFS(AD), IN' -> 'Chennai, MUM-KND-AFS(AD), India'."""
    if not raw:
        return "India"
    normalised = re.sub(r",\s*IN\s*$", ", India", raw.strip())
    if "india" not in normalised.lower():
        normalised = f"{normalised}, India"
    return normalised


def _fetch_page(keyword: str, start: int, timeout: int) -> str:
    params: dict[str, object] = {"q": keyword, "locationsearch": "India"}
    if start:
        params["startrow"] = start

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Mahindra: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Mahindra search failed: {exc}") from exc
    raise RateLimitError(f"Mahindra search: no response -- {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Mahindra & Mahindra India jobs matching *keyword*.

    ``num`` is accepted for contract compatibility but this tenant's real
    page size is a fixed 10 (see module docstring) -- ``start`` is honored
    exactly, ``num`` is not otherwise enforced beyond that.
    """
    html_text = _fetch_page(keyword, start, timeout)
    soup = BeautifulSoup(html_text, "html.parser")

    if _NO_MATCH_LABEL in soup.get_text(" ", strip=True).lower():
        # This tenant silently swaps in an unrelated "25 most recent jobs"
        # list once a keyword has no more real matches (even mid-pagination
        # for a keyword that DID have real earlier pages) -- treat exactly
        # like an empty page, never consume the fallback rows below.
        return []

    rows = soup.select("tr.data-row")
    if not rows:
        return []

    jobs: list[dict] = []
    ids_this_page: set[str] = set()
    for row in rows:
        link = row.select_one("span.jobTitle.hidden-phone a.jobTitle-link")
        if not link:
            link = row.select_one("a.jobTitle-link")
        if not link:
            continue

        href = (link.get("href") or "").strip()
        title = html_mod.unescape(link.get_text(strip=True))
        if not href or not title:
            continue

        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id.isdigit():
            continue
        ids_this_page.add(job_id)

        loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
        if not loc_cell:
            loc_cell = row.select_one("span.jobLocation")
        loc_text = html_mod.unescape(loc_cell.get_text(strip=True)) if loc_cell else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": _normalise_location(loc_text),
            "posting_date": "",  # not present in search results; filled on detail fetch
            "application_url": f"{_BASE_URL}{href}",
        })

    if start == 0:
        _FIRST_PAGE_IDS[keyword] = ids_this_page
    elif ids_this_page and _FIRST_PAGE_IDS.get(keyword) == ids_this_page:
        return []  # ATS silently replayed this keyword's first page

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description and posting date from a detail page.

    Returns (description_text, posting_date) where posting_date is
    YYYY-MM-DD. Same selectors as Birlasoft's classic-theme J2W tenant:
    <span class="jobdescription"> for the full JD and
    <meta itemprop="datePosted" content="Thu Aug 13 00:00:00 UTC 2026">.
    """
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Mahindra description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Mahindra description fetch failed: {exc}") from exc

    if r is None:
        return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    desc_span = soup.select_one("span.jobdescription")
    description = ""
    if desc_span:
        raw = html_mod.unescape(desc_span.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    date_meta = soup.find("meta", {"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_detail_date(date_meta.get("content", ""))

    return description, posting_date
