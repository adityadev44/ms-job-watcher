"""Fetches Nutanix job listings via its Jobvite career site.

Step 1 ATS identification done from scratch (not trusted from prior secondary
research, which had guessed "Greenhouse-adjacent" -- wrong). `careers.nutanix.com`
itself sits behind a Cloudflare *managed challenge* (plain `requests` gets a
403 "Just a moment..." interstitial, no API reachable without solving JS).
But the actual job-board content lives on a separate, un-gated Jobvite
domain: `jobs.jobvite.com/nutanix` (company id `qKr9VfwZ`, footer literally
says "Powered by Jobvite"). That domain has no Cloudflare/bot-protection at
all -- plain `requests` with a browser UA works immediately. Jobvite is a new
ATS vendor for this repo (not seen in any of the prior ~137 integrations).

The board is plain server-rendered HTML (Angular is used only for the
homepage's facet dropdowns, not for the actual `/search` results -- view-
source on `/nutanix/search?q=...` shows real `<tr>` rows with no JS needed),
so no Playwright is required, unlike the Cloudflare-gated front door.

Search: `GET /nutanix/search?q=<keyword>&p=<page>`.
- `q` genuinely narrows results server-side (confirmed: a nonsense keyword
  returns "No results found for '...'", "angular" returns 0, "engineer"
  returns 96 of 227 total) -- but it does loose OR/token matching, not exact
  phrase (e.g. "C# developer" returns the same broad "*developer*" postings
  as "python developer" -- the "C#" token itself has no special effect).
  This is the same "loose pre-filter, real precision comes from matcher.py's
  title/skill checks" pattern already documented for Nagarro/NEC/PhonePe --
  not a bug, no code needs to work around it.
- Pagination is a discrete page *number* (`p=0`, `p=1`, ...), not an
  arbitrary row offset -- unlike most Workday/J2W tenants in this repo. Fixed
  50 rows/page. An out-of-range page returns a clean "No results found" (HTTP
  200, 0 rows) -- no UBS/MUFG/Nvidia-style silent wraparound.
- The India-specific `l=` location facet exists but is unreliable for the
  generic value this repo's config uses: `l=India` returns "No results
  found" even though real India jobs exist (confirmed: `l=Bangalore, India`
  -- the exact composite facet value, comma *and* space required -- filters
  correctly to 34/34 genuine Bangalore postings). Since config.yaml's shared
  `india_locations` anchor is just `["India"]`, not a per-company exact facet
  string, this fetcher does not use `l=` at all: it fetches globally per
  keyword and lets `matcher.py`'s `is_india_job()` do the real filtering,
  the same conservative choice already made for Genpact/Fiserv/FactSet/WTW.
  Location text in results is clean "City,\\nCountry" (e.g. "Bangalore,\\nIndia")
  with no facet-leakage quirk like Micron/Verizon/Lowe's.
- Known, unfixed edge case (same shape as the Maersk "2 Locations" bug,
  deliberately not patched here): postings open in more than one office show
  a bare "N Locations" pill with no city/country text at all -- if India is
  one of the N without a dedicated single-country posting, it is invisible
  to `is_india_job()` and silently skipped. Surveyed the full 227-job global
  pool: all ~51 such multi-location rows are Sales/Account-Manager titles,
  none would pass `title_family` anyway, so this costs zero real matches
  today -- flagged per the "flag, don't silently patch" discipline in case
  that ever changes.

Job detail pages (`/nutanix/job/<id>`) are also plain server-rendered HTML:
full JD in `div.jv-job-detail-description`, title in `h2.jv-header`. No
posting-date field exists anywhere on this ATS (no meta tag, no JSON-LD, not
even in the search listing) -- same as IBM/United Airlines, `posting_date`
is returned as `""`.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.jobvite.com/nutanix"
_SEARCH_URL = f"{_BASE_URL}/search"
_JOBVITE_PAGE_SIZE = 50  # fixed by Jobvite; the `num` argument is not honoured

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
    """Raised on 429 or persistent connection failure from Nutanix's Jobvite board."""


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _JOBVITE_PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Fetch one page of Nutanix jobs for the given keyword.

    ``location`` is accepted for API compatibility but not used as a request
    filter -- see module docstring for why (the generic "India" facet value
    this repo's config uses doesn't work on this tenant). India is filtered
    client-side by the caller via ``matcher.is_india_job``. ``num`` is also
    accepted for compatibility; Jobvite always returns up to 50 rows/page.
    ``start`` is treated as a row offset and mapped to Jobvite's page-number
    pagination (``page = start // 50``).
    """
    page_number = start // _JOBVITE_PAGE_SIZE
    params: dict = {"p": page_number}
    if keyword:
        params["q"] = keyword

    for attempt in range(3):
        try:
            r = requests.get(
                _SEARCH_URL,
                params=params,
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Nutanix: 429 rate-limited for '{keyword}'")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Nutanix search failed for '{keyword}': {exc}") from exc

    soup = BeautifulSoup(r.text, "html.parser")

    jobs: list[dict] = []
    for name_cell in soup.select("td.jv-job-list-name"):
        link = name_cell.select_one("a[href]")
        if not link:
            continue
        href = link.get("href", "").strip()
        title = html_mod.unescape(link.get_text(strip=True))
        if not href or not title:
            continue

        # Job ID: trailing path segment, e.g. "/nutanix/job/okIIAfw4" -> "okIIAfw4"
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id:
            continue

        loc_cell = name_cell.find_next_sibling("td", class_="jv-job-list-location")
        loc_text = ""
        if loc_cell:
            # "City,\n            Country" (or a bare "N Locations" pill with
            # no place name at all) -- collapse whitespace, keep as "City, Country".
            raw_loc = html_mod.unescape(loc_cell.get_text(" ", strip=True))
            loc_text = re.sub(r"\s*,\s*", ", ", " ".join(raw_loc.split()))

        jobs.append(
            {
                "id": job_id,
                "title": title,
                "location": loc_text,
                "posting_date": "",
                "application_url": f"{_BASE_URL}/job/{job_id}",
            }
        )

    # Stop-signal for the tail page: Jobvite's page number is `start // 50`,
    # which stays *constant* across every `start` value inside the last
    # partial page (e.g. start=150..199 all map to page 3 of a 162-result
    # search) -- matcher.py's caller keeps incrementing `start` by the
    # returned page length and would otherwise re-request that same partial
    # page repeatedly until `start` reaches `max_listings` (harmless with the
    # default max_listings=200, but a real risk of dozens of redundant
    # requests with a larger config, or if a trailing page is very small).
    # Once we've already told the caller about every row this query has
    # (start >= total), return [] instead of the same rows again so the
    # caller's own `if not page: break` ends pagination in one extra call.
    total_match = re.search(r"(\d+)-(\d+)\s+of\s+(\d+)", soup.get_text(" ", strip=True))
    if total_match:
        total = int(total_match.group(3))
        if start >= total:
            return []

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the full job description from a Nutanix Jobvite detail page.

    Returns (description_text, posting_date) -- posting_date is always ""
    since no date field (meta tag, JSON-LD, or listing column) exists
    anywhere on this ATS.
    """
    for attempt in range(3):
        try:
            r = requests.get(
                application_url,
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                raise RateLimitError(f"Nutanix description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    desc_div = soup.select_one("div.jv-job-detail-description")
    description = ""
    if desc_div:
        raw = html_mod.unescape(desc_div.get_text(" ", strip=True))
        description = " ".join(raw.split())

    return description, ""
