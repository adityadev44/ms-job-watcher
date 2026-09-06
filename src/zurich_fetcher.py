"""Fetches Zurich Insurance job listings via SAP SuccessFactors J2W (classic).

Zurich Insurance Group's careers portal (www.careers.zurich.com) runs on
SAP SuccessFactors Jobs2Web (J2W) — the classic server-rendered theme.
Same ATS family as Swiss Re, Nomura, Capgemini, SAP Labs, and Mastek in
this repo. Confirmed live (2026-09-06) by the careers.zurich.com page
source: explicit SuccessFactors CDN references for jQuery, j2w CSS/JS, and
`<meta itemprop="datePosted">` on job detail pages (identical J2W markers
across all four existing J2W tenants here).

Key facts confirmed via direct API probes (2026-09-06):

- **Location filter**: `locationsearch=india` (query-string param) is a
  genuine server-side India filter — tested A/B: no filter → 753 total
  jobs; `locationsearch=us` → 134 US jobs; `locationsearch=india` → 0
  India jobs. The Zurich GCC in Hyderabad was only officially launched in
  April 2026 (Fiinews.com), with Amit Kalra appointed as Global Head of
  Zurich Capability Centers effective 1 July 2026 — meaning the public
  job board for India has 0 postings at investigation time (2026-09-06).
  The fetcher is implemented now so it goes live automatically once India
  jobs are posted.
- **Keyword filter**: `q=<keyword>` genuinely narrows server-side (verified:
  `q=software` → 92 of 753 total; a nonsense query → 1; `q=` → 753).
  NOT in `_IGNORES_KEYWORDS`.
- **Pagination**: query-string based (`?startrow=N`), NOT path-based like
  Swiss Re/Nomura. Page size is fixed at 25 rows per page (standard J2W
  classic). `startrow=0` is implicit on the first page.
- **Data row structure**: `<tr class="data-row">` containing a
  `<a class="jobTitle-link" href="/job/{location-slug}-{title-slug}/{id}/">` anchor,
  a `<span class="jobLocation">City, CC</span>`, and a
  `<span class="jobDate">Mon Day, YYYY</span>`.
- **Job ID**: the 10-digit integer at the end of the job URL path, e.g.
  `/job/.../1354982057/` → "1354982057".
- **Location format**: "City Name, CC" where CC is a 2-letter country code,
  e.g. "Hyderabad, IN" for India. For India the filter guarantees CC=="IN",
  but the fetcher also client-side guards on ", IN" or "india".
- **Descriptions**: fetched from the job detail page via the same J2W
  pattern as Swiss Re/Nomura: `<span class="jobdescription">` for the body
  and `<meta itemprop="datePosted" content="...">` for the posting date.
  Date format: "Www Mon DD HH:MM:SS UTC YYYY" (e.g. "Mon Aug 31 02:00:00
  UTC 2026").
- **Wraparound**: not observed; pages past the last return 0 data-rows cleanly.

India coverage: 0 jobs at investigation time (GCC brand-new). Fetcher will
self-activate once Zurich posts India roles. No `require_tech_in_description`
set now — board is too new to characterise signal quality; revisit once ≥20
India jobs appear.
"""

from __future__ import annotations

import html as _html_mod
import re
import time
from datetime import datetime

import requests

_BASE_URL = "https://www.careers.zurich.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # fixed by J2W classic theme

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Pagination guard: store first-page job IDs per keyword.
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on HTTP 429 or persistent connection failure from careers.zurich.com."""


# ---------------------------------------------------------------------------
# Date helpers — J2W classic formats
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert 'Aug 31, 2026' (search-row jobDate) to 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Mon Aug 31 02:00:00 UTC 2026' (meta datePosted) to 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_location(raw: str) -> str:
    """Normalise 'Hyderabad, IN' → 'Hyderabad, India'; 'IN' → 'India'."""
    loc = (raw or "").strip()
    if not loc:
        return "India"
    if loc.upper() == "IN":
        return "India"
    # Replace a trailing ', IN' (country code) with ', India'.
    normalized = re.sub(r",\s*IN$", ", India", loc)
    return normalized


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_with_retries(url: str, params: dict | None, timeout: int, label: str) -> str:
    """GET `url` with retry-on-failure; returns response text."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Zurich J2W {label}: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zurich J2W {label} failed: {exc}") from exc
    raise RateLimitError(f"Zurich J2W {label}: no response — {last_exc}")


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
    """Return a page of Zurich India job postings via J2W HTML scraping.

    `keyword` narrows server-side via `q=`. `location` is ignored — the
    India filter is applied server-side via `locationsearch=india` always.
    `start` is the row offset (0, 25, 50, …); `num` is ignored since J2W
    returns a fixed 25 rows per page.
    """
    global _FIRST_PAGE_IDS

    params: dict[str, str | int] = {
        "q": keyword,
        "locationsearch": "india",
        "sortColumn": "referencedate",
        "sortDirection": "desc",
    }
    if start > 0:
        params["startrow"] = start

    html_text = _get_with_retries(_SEARCH_URL, params, timeout, "search")

    # Find the table total (used to decide whether to request more pages).
    total_m = re.search(r"of\s*<b>(\d+)</b>", html_text)
    _total = int(total_m.group(1)) if total_m else None  # noqa: F841

    # Parse data-rows.
    row_re = re.compile(r'<tr class="data-row">(.*?)</tr>', re.DOTALL)
    rows = row_re.findall(html_text)

    jobs: list[dict[str, str]] = []
    for row in rows:
        # Title link: <a href="/job/{slug}/{id}/" class="jobTitle-link">Title</a>
        link_m = re.search(
            r'<a\s+href="(/job/[^"]+/(\d+)/)"[^>]*class="jobTitle-link"[^>]*>\s*([^<]+)',
            row,
        )
        if not link_m:
            # Alternate attribute order: class before href
            link_m = re.search(
                r'<a\s+class="jobTitle-link"\s+href="(/job/[^"]+/(\d+)/)"[^>]*>\s*([^<]+)',
                row,
            )
        if not link_m:
            continue

        relative_url = link_m.group(1)
        job_id = link_m.group(2)
        title = _html_mod.unescape(link_m.group(3).strip())

        if not job_id or not title:
            continue

        # Location: pick the colLocation td's span (not the visible-phone duplicate).
        loc_m = re.search(
            r'<td[^>]+class="colLocation[^"]*"[^>]*>.*?<span[^>]+class="jobLocation[^"]*"[^>]*>\s*(.*?)\s*</span>',
            row,
            re.DOTALL,
        )
        if loc_m:
            loc_raw = _html_mod.unescape(loc_m.group(1).strip())
        else:
            # Fallback: first jobLocation span anywhere in the row.
            loc_any = re.search(r'<span[^>]+class="jobLocation[^"]*"[^>]*>\s*(.*?)\s*</span>', row, re.DOTALL)
            loc_raw = _html_mod.unescape(loc_any.group(1).strip()) if loc_any else ""

        loc = _normalize_location(loc_raw)

        # Client-side India guard (locationsearch should guarantee this already).
        if "india" not in loc.lower() and ", in" not in loc_raw.lower():
            if "india" not in loc_raw.lower():
                continue

        # Date: colDate td's span.
        date_m = re.search(
            r'<td[^>]+class="colDate[^"]*"[^>]*>.*?<span[^>]+class="jobDate[^"]*"[^>]*>\s*([^<]+)',
            row,
            re.DOTALL,
        )
        posting_date = ""
        if date_m:
            posting_date = _parse_search_date(date_m.group(1).strip())

        app_url = f"{_BASE_URL}{relative_url}"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "application_url": app_url,
            "posting_date": posting_date,
        })

    # Pagination guard.
    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []  # wraparound detected

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch description and posting date from a Zurich J2W job detail page.

    Detail page uses the standard J2W classic markers:
    - `<span class="jobdescription">` for the description body.
    - `<meta itemprop="datePosted" content="Www Mon DD HH:MM:SS UTC YYYY">` for the date.
    """
    html_text = _get_with_retries(application_url, None, timeout, "description")

    # Description body.
    desc_m = re.search(
        r'<span[^>]+class="jobdescription"[^>]*>(.*?)</span>',
        html_text,
        re.DOTALL,
    )
    description = ""
    if desc_m:
        description = _strip_html(desc_m.group(1))

    # Posting date from meta tag.
    date_m = re.search(r'<meta[^>]+itemprop="datePosted"[^>]+content="([^"]+)"', html_text)
    posting_date = _parse_detail_date(date_m.group(1)) if date_m else ""

    return description, posting_date
