"""Swiss Re job fetcher — SAP SuccessFactors J2W (classic) HTML scraping.

Careers site: careers.swissre.com — classic (non-Unify) J2W theme, same
family as Nomura/Capgemini/SAP Labs/Mastek: view source on
`/go/Search-Jobs/2744601/` shows real server-rendered `<tr class="data-row">`
rows on the very first load, no CSRF/session dance needed. Confirmed live
2026-08-31 via the homepage's own CSP header, which whitelists
`rmk-map-12.jobs2web.com`/`rmkcdn.successfactors.com`/`*.sapsf.com`/
`*.successfactors.com` and sets `frame-ancestors ... career2.successfactors.eu`.

`/go/Search-Jobs/2744601/` accepts a plain GET with `q=<keyword>` and
`locationsearch=india`. Unlike Mastek's broken fallback, Swiss Re's keyword
param genuinely narrows server-side (verified live: `q=software` -> 4 of 33
India jobs; a nonsense keyword -> 0 rows, no silent fallback to a default
set). Ignored anyway here: the whole India pool is only ~33 jobs total, far
cheaper to cache once (2 pages) and let matcher.py's shared title/skill
filters do the real narrowing across all 10 configured keywords — same
"cache the small pool once" pattern as Mastek/CRED/Groww/Meesho/Razorpay.

Pagination is PATH-based, not query-string (`?startrow=N` a la
Capgemini/Mastek): page 1 is `/go/Search-Jobs/2744601/`, page 2 is
`/go/Search-Jobs/2744601/25/`, page 3 would be `/go/Search-Jobs/2744601/50/`
— identical mechanism to Nomura, the literal path segment is the row offset,
page size fixed at 25.

Search-result locations are "City, ST, IN" (e.g. "Bangalore, KA, IN") — a
state code is always present, the bare word "India" never appears —
normalised by stripping the trailing ", ST, IN" down to ", India" so
matcher.py's is_india_job() recognises them: "Bangalore, KA, IN" ->
"Bangalore, India". Live sample was 100% genuine India cities (Bangalore/
Mumbai/Hyderabad) with no non-India leakage through `locationsearch=india`.

Job-detail pages are plain server-rendered HTML: `span.jobdescription` for
the JD body, `meta[itemprop="datePosted"]` (format
"Mon Aug 31 00:00:00 UTC 2026") for the posting date — identical anchors to
Nomura/Capgemini/Mastek/SAP Labs. Only one `itemprop="description"` per page
here (unlike Wipro/HCLTech's Unify theme, which duplicates it onto an
unrelated company blurb — not a concern on this classic-theme tenant).

Titles are mostly generic insurance/ops bands (Actuarial Analyst, Financial
Analyst III, UW Advisor) with a handful of engineering-titled roles that are
themselves generic and stack-ambiguous: live samples included "Application
Engineer I" (SAP S4HANA/Java/Azure/Postgres stack, zero .NET/C# signal in the
JD body), "ESM ServiceNow DevSecOps Engineer II" (pure ServiceNow platform
work), "IT Service Engineer II" (vendor/incident-management ops for a
Treasury Collateral Management platform, no dev-stack mention at all), and
"Automation engineer" (Magnum rules-engine/UiPath automation with one soft
"Generative AI tools to improve...testing" mention, no LangChain/RAG/vector-
DB stack). None of the live sample's engineering roles are actually about
.NET/C# or AI/ML/Python engineering — `require_tech_in_description` is
enabled as a precision tightener, same rationale as Sabre/Autodesk/Adobe/
eBay's generic-title direct-employer pattern.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.swissre.com"
_SEARCH_BASE = f"{_BASE_URL}/go/Search-Jobs/2744601"
_SEARCH_QS = "q=&locationsearch=india&sortColumn=referencedate&sortDirection=desc"
_PAGE_SIZE = 25  # fixed by this J2W theme; confirmed via "Results 1-25 of 33"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Swiss Re's J2W tenant."""


# Module-level cache: the India pool is small (~33 jobs) and keyword
# filtering isn't trustworthy to rely on for 10 separate keyword passes, so
# fetch the whole pool once and serve fetch_jobs()/fetch_job_description()
# from it — same pattern as Mastek/Nomura/CRED/Groww.
_job_cache: list[dict] = []
_cache_filled: bool = False


# ---------------------------------------------------------------------------
# Date / location helpers
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert '31 Aug 2026' (search row) to '2026-08-31'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Mon Aug 31 00:00:00 UTC 2026' (meta tag) to '2026-08-31'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_location(raw: str) -> str:
    """'Bangalore, KA, IN' -> 'Bangalore, India'; 'IN' -> 'India'."""
    loc_text = (raw or "").strip()
    if not loc_text:
        return "India"
    if loc_text.upper() == "IN":
        return "India"
    # Drop a trailing ", <state code>, IN" (or just ", IN") down to ", India"
    normalized = re.sub(r",\s*[A-Z]{2},\s*IN$", ", India", loc_text)
    normalized = re.sub(r",\s*IN$", ", India", normalized)
    return normalized


# ---------------------------------------------------------------------------
# Cache fill
# ---------------------------------------------------------------------------

def _fetch_page(start: int, timeout: int) -> str:
    """GET one /go/Search-Jobs/2744601/ page; 3-attempt retry with backoff."""
    url = _SEARCH_BASE + "/" if start == 0 else f"{_SEARCH_BASE}/{start}/"
    url = f"{url}?{_SEARCH_QS}"

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Swiss Re J2W: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Swiss Re search fetch failed: {exc}") from exc

    raise RateLimitError(f"Swiss Re search fetch: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire India job pool once; cache job list.

    _cache_filled is set before the request attempts so a transient failure
    doesn't trigger a retry storm on every fetch_jobs() call made during the
    same process run (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    total: int | None = None
    start = 0

    while True:
        html_text = _fetch_page(start, timeout)

        if total is None:
            m = re.search(r"of\s*<b>(\d+)</b>", html_text)
            if m:
                total = int(m.group(1))

        soup = BeautifulSoup(html_text, "html.parser")
        rows = soup.select("tr.data-row")
        if not rows:
            break

        new_this_page = 0
        for row in rows:
            link = row.select_one("a.jobTitle-link")
            if not link:
                continue
            href = (link.get("href") or "").strip()
            title = html_mod.unescape(link.get_text(strip=True))
            if not href or not title:
                continue

            job_id = href.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit() or job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            loc_span = row.select_one("td.colLocation span.jobLocation") or row.select_one(
                "span.jobLocation"
            )
            loc_text = _normalize_location(
                html_mod.unescape(loc_span.get_text(strip=True)) if loc_span else ""
            )

            date_span = row.select_one("td.colDate span.jobDate") or row.select_one(
                "span.jobDate"
            )
            posting_date = _parse_search_date(
                date_span.get_text(strip=True) if date_span else ""
            )

            jobs.append({
                "id": job_id,
                "title": title,
                "location": loc_text,
                "posting_date": posting_date,
                "application_url": f"{_BASE_URL}{href}",
            })
            new_this_page += 1

        start += _PAGE_SIZE
        if total is not None and len(seen_ids) >= total:
            break
        if new_this_page == 0:
            break
        if start > 2000:  # safety cap; never expected for a ~33-job pool
            break
        time.sleep(0.2)

    _job_cache[:] = jobs
    print(f"[Swiss Re] Cache filled: {len(jobs)} India jobs")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Swiss Re India jobs.

    Keyword/location are accepted but ignored — the India pool is small
    enough (~33 jobs) that caching it once and letting the shared matcher's
    title/skill filters narrow it is cheaper and safer than trusting 10
    separate server-side keyword passes (see module docstring).
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Swiss Re job.

    Detail page uses <span class="jobdescription"> and
    <meta itemprop="datePosted" content="Mon Aug 31 00:00:00 UTC 2026">.
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    f"Swiss Re description: 429 rate-limited for {application_url}"
                )
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Swiss Re description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Swiss Re description fetch: no response — {last_exc}")

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
