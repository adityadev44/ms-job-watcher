"""Fetches YASH Technologies (careers.yash.com) India job listings via the
SAP SuccessFactors J2W HTML search API -- the classic (non-Unify) J2W
"data-row" theme, same product family/DOM shape as Capgemini/Nomura/SAP
Labs/Mastek. Confirmed live 2026-09-04 via plain `curl`: view-source on
`/search/` shows real server-rendered `<tr class="data-row">` rows already
present on first load (no CSRF token, no session cookie, no JS execution
needed -- this is NOT the Unify REST-API theme used by Standard
Chartered/Wipro/HCLTech/Wipro).

`/search/` accepts `q=<keyword>`, `locationsearch=india` (reliable
server-side filter -- every sampled row across all 32 pages of the full
315-job pool showed a genuine India city: Pune/Bangalore/Hyderabad/Indore,
or a bare "IN" country code with no city, never a non-India place), and
`startrow=N` for pagination (10 results/page, confirmed via the page's own
"Results 1 - 10 of 315" label and "Page 1 of 32" hint).

IMPORTANT: `q=` genuinely changes the result count per query (e.g. "C#" ->
44 of 315, "SDE" -> 4 of 315), so this is NOT a param the tenant silently
ignores -- but it is a noisy, unreliable OR-of-words full-text match, not an
AND/phrase match: "machine learning engineer" and "generative ai engineer"
each return 314 of 315 (essentially the *entire* pool, since "engineer" by
itself matches almost every title on this tenant). Using it as the real
narrowing signal would be actively misleading (10 near-identical passes
returning almost the whole pool every time), so -- same decision already
made for Persistent/Dover/Mastek/Iris Software on this same ATS family --
keywords are intentionally ignored and the full pool is cached once per
process; the shared matcher does the real title/skill narrowing.

Location text on both search rows and detail pages is "<City>, <ST>, IN" /
"<City>, IN" / bare "IN" (never the literal word "India"), so it's
normalised to "<City>, ST, India" / "India" before being handed to
matcher.py's `is_india_job()` (same normalisation shape as Capgemini's
", IN" -> ", India", generalised here to also cover the bare "IN" case that
Capgemini's tenant never produces). This is safe to do unconditionally
because every row in this cache was already filtered server-side via
`locationsearch=india` -- unlike Micron/Verizon/Lowe's, no non-India leakage
was observed in a full live pull of all 315 rows.

Many rows are posted at more than one location ("Bangalore, KA, IN
<small>+1 more...</small>"); only the first (non-"+N more") text node is
kept, same approach Capgemini's fetcher already uses for this exact DOM
shape -- iterating `.children` rather than `get_text()`, since a flattened
`get_text()` would silently glue the "+1 more..." suffix onto the location
string.

Titles are level-banded IT-services style ("Sr. Software Engineer - <Tech>
Job", "Module Lead - <Tech> Job", "Tech Lead - <Tech> Job") but -- like NEC
Software Solutions / Iris Software and UNLIKE Wipro/HCLTech/DXC -- the tech
stack is named directly in the title itself (".NET C#", "AI", "Databricks",
"Java", "Python", "MES Aspen", ...). Sampled multiple non-.NET/non-AI
detail-page descriptions (SAP FICO, MES Aspen) for the Walmart-style
"languages we also use" boilerplate false-positive risk and found none --
every description stayed on-topic for its own stack. Combined with the
`.NET / C#` `primary_skills` group already being narrow (no bare SQL
Server/Web API terms), `require_tech_in_description` is deliberately NOT
enabled here for the same reason as Iris Software/NEC: titles already
self-disclose their stack, so Layer 4's generic-title false-positive risk is
structurally smaller at this tenant than at Wipro/HCLTech/TCS.

Every `itemprop="description"` detail page checked had exactly one match
(no Wipro/HCLTech-style duplicate-itemprop boilerplate trap).

Search-row and detail-page dates share the same two formats as
Capgemini/Iris/Mastek: "Aug 28, 2026" (search) and
"Thu Aug 28 00:00:00 UTC 2026" (`meta[itemprop="datePosted"]`, detail page,
used as the authoritative value).
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.yash.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 10  # confirmed via "Results 1 - 10 of N" on this tenant

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from YASH's J2W tenant."""


# Module-level cache: the full India pool is small (~315 jobs) and keywords
# aren't a reliable narrowing signal on this tenant (see module docstring),
# so it's fetched once per process and fetch_jobs() is served from it after.
_job_cache: list[dict] = []
_cache_filled: bool = False


# ---------------------------------------------------------------------------
# Date / location helpers
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert 'Aug 28, 2026' (search result) to '2026-08-28'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Thu Aug 28 00:00:00 UTC 2026' (meta tag) to '2026-08-28'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_location(raw: str) -> str:
    """'Bangalore, KA, IN' -> 'Bangalore, KA, India'; bare 'IN' -> 'India'.

    Every row this is applied to already came from a `locationsearch=india`
    server-side-filtered response, so an unconditional trailing-"IN" ->
    "India" swap is safe here (unlike Micron/Verizon, where the facet itself
    was unreliable and blind normalisation would have mislabelled genuine
    non-India rows).
    """
    text = raw.strip()
    if not text:
        return "India"
    text = re.sub(r"\bIN\b\s*$", "India", text)
    if "india" not in text.lower():
        text = f"{text}, India"
    return text


def _first_location_text(loc_cell) -> str:
    """Extract just the primary location, skipping '+N more...' siblings.

    Mirrors Capgemini's fetcher: a flattened get_text() would glue the
    "+1 more..." suffix straight onto the city text.
    """
    if loc_cell is None:
        return ""
    inner = loc_cell.select_one("span.jobLocation") or loc_cell
    for part in inner.children:
        raw_part = getattr(part, "string", None) or (
            str(part) if not hasattr(part, "children") else ""
        )
        candidate = (raw_part or "").strip()
        if candidate and not candidate.startswith("+"):
            return html_mod.unescape(candidate)
    return ""


# ---------------------------------------------------------------------------
# Cache fill
# ---------------------------------------------------------------------------

def _fetch_page(start: int, timeout: int) -> str:
    """GET one page of India job rows; 3-attempt retry with exponential backoff."""
    params = {
        "q": "",
        "locationsearch": "india",
        "sortColumn": "referencedate",
        "sortDirection": "desc",
    }
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
                raise RateLimitError("YASH J2W: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"YASH search fetch failed: {exc}") from exc

    raise RateLimitError(f"YASH search fetch: no response -- {last_exc}")


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
            if not job_id.isdigit() or job_id in seen_ids:
                continue

            loc_cell = row.select_one("td.colLocation.hidden-phone")
            loc_text = _normalize_location(_first_location_text(loc_cell))

            date_span = row.select_one("td.colDate.hidden-phone span.jobDate")
            if not date_span:
                date_span = row.select_one("span.jobDate")
            posting_date = _parse_search_date(date_span.get_text(strip=True)) if date_span else ""

            seen_ids.add(job_id)
            jobs.append({
                "id": job_id,
                "title": title,
                "location": loc_text,
                "posting_date": posting_date,
                "application_url": f"{_BASE_URL}{href}",
            })

        start += _PAGE_SIZE
        if total is not None and len(seen_ids) >= total:
            break
        if start > 3000:
            break
        time.sleep(0.2)

    _job_cache[:] = jobs
    print(f"[YASH Technologies] Cache filled: {len(jobs)} India jobs")


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
    """Return a page of YASH Technologies India jobs.

    keyword/location are accepted but ignored -- the full India pool is
    small (~315 jobs) and this tenant's own `q=` search is a noisy
    OR-of-words match (see module docstring), so it is cached once per
    process and the shared matcher does the real title/skill filtering.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single YASH job.

    Detail page uses <span class="jobdescription"> (single occurrence,
    verified -- no Wipro/HCLTech-style duplicate itemprop) and
    <meta itemprop="datePosted" content="Thu Aug 28 00:00:00 UTC 2026">.
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
                raise RateLimitError(f"YASH description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"YASH description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"YASH description fetch: no response -- {last_exc}")

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
