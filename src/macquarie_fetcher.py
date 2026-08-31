"""Fetches Macquarie Group job listings via recruitment.macquarie.com (Avature ATS).

**Correction vs. the "confirmed Workday" starting assumption**: the obvious
guess -- `mq.wd3.myworkdayjobs.com/CareersatMQ` -- IS live Workday and DOES
return real jobs, but it belongs to **Macquarie University** (Sydney), not
Macquarie Group (the investment bank / asset manager this pipeline actually
wants). Confirmed live: every posting there is academic/administrative
("Research Fellow - China Studies", "Wallumattagal Campus" -- MQ's own
campus), only 25 total postings, apply links literally say
"Macquarie-University". Macquarie Group's real "Search Jobs" button (from
https://www.macquarie.com/au/en/careers.html) links to
recruitment.macquarie.com, whose page source contains `avature.portal.*` JS
globals -- it's Avature, a different ATS entirely. Same-name/different-org
collision -- worth flagging if any other "obvious" tenant guess is ever
taken on faith without hitting it live first.

ATS behavior (recruitment.macquarie.com, Avature):
- Search: GET
    https://recruitment.macquarie.com/en_US/careers/SearchJobs/{urlencoded keyword}
        ?listFilterMode=1&jobRecordsPerPage=9&jobOffset={N}
  Plain server-rendered HTML -- no JS/Playwright needed. `jobRecordsPerPage`
  is NOT honoured above the site's fixed page size of 9; pagination must
  walk `jobOffset` in steps of 9 regardless of the value requested.
- Unlike most Workday tenants in this repo, the free-text `search` box DOES
  filter server-side by keyword -- result totals differ meaningfully per
  keyword ("software engineer" -> 74, "AI engineer" -> 71, "C# developer"
  -> 2, ".NET developer" -> 0). `#` is safely URL-encoded by
  `urllib.parse.quote` and does not break search the way it did for TCS's
  iBegin portal -- no special-case workaround needed here.
- No location/country facet is usable: the site's "Countries"/"Cities"
  fields only populate via an opaque AJAX autocomplete (numeric IDs, not
  discoverable from a plain GET). Appending "india" as a second search term
  looked promising (`search="{keyword} india"` narrowed "software engineer"
  74 -> 15) but is UNRELIABLE -- a side-by-side ID-set diff against the
  keyword-only result set showed it silently dropped 5 genuine India
  postings and pulled in 1 that wasn't India. Do not use that shortcut.
- Every India posting is one of exactly two offices -- "Gurugram Office" or
  "Hyderabad Office" -- verified by paging through several keywords' full
  unfiltered result sets and cross-checking against a `search=india` query,
  which independently surfaced the same two office names only. Location
  text never contains the word "India" itself, so ", India" is appended
  only after a city-name whitelist match (Lowe's/Invesco pattern), not
  blindly -- most Macquarie postings are Sydney/London/Singapore/etc.
- Posting dates are already absolute ("11 Feb 2026" in search results,
  "11-Feb-2026" on the detail page) -- no relative "Posted N Days Ago"
  parsing needed, unlike the Workday fetchers in this repo.
- The application URL (`.../JobDetail?jobId=N`) is itself a plain
  server-rendered HTML detail page carrying the full description text
  across one or more `<article class="article--details">` blocks -- the
  same URL serves both the apply link and the description fetch; there is
  no separate JSON detail API.

Because the search endpoint genuinely filters by keyword server-side, each
keyword's full India-filtered result set is fetched once (paginating
`jobOffset` in steps of 9) and cached in-module, then re-sliced by
(start, num) for matcher.py's repeated per-keyword page calls.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_BASE = "https://recruitment.macquarie.com"
_SEARCH_BASE = f"{_BASE}/en_US/careers/SearchJobs"

_SITE_PAGE_SIZE = 9  # fixed by the site; jobRecordsPerPage is not honoured above this
_MAX_PAGES_PER_KEYWORD = 60  # defensive cap (~540 raw jobs) against runaway pagination

# The only two India offices observed across every keyword tried, cross-checked
# against an independent `search=india` query. Location text never says
# "India" itself, so this whitelist decides which raw results get kept.
_INDIA_CITY_TOKENS = ("gurugram", "hyderabad")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from Macquarie's site."""


# Module-level cache: filled once per keyword, reused across matcher.py's
# repeated (start, num) page calls for that same keyword.
_cache: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# Date helper -- Macquarie's dates are already absolute, unlike Workday's
# relative "Posted N Days Ago" strings.
# ---------------------------------------------------------------------------

def _parse_posted_on(text: str) -> str:
    """Convert Macquarie's absolute date strings to YYYY-MM-DD.

    Search results use "11 Feb 2026"; the detail page uses "11-Feb-2026".
    """
    if not text:
        return ""
    text = text.strip()
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# HTTP helper with retry/backoff
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Macquarie: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Macquarie request failed: {exc}") from exc
    raise RateLimitError(f"Macquarie request failed: {last_exc}")


# ---------------------------------------------------------------------------
# Search-results page parsing
# ---------------------------------------------------------------------------

def _parse_search_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []

    for article in soup.find_all("article", class_="article--result"):
        link = article.select_one(".article__header a.link")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not (title and href):
            continue

        job_id = ""
        loc = ""
        date_text = ""
        for data_div in article.select("div.article__details__data"):
            img = data_div.find("img")
            p_tags = data_div.find_all("p")
            if img is not None:
                alt = (img.get("alt") or "").lower()
                value = p_tags[-1].get_text(strip=True) if p_tags else ""
                if "location" in alt:
                    loc = value
                elif "date" in alt:
                    date_text = value
            else:
                icon_p = data_div.find("p", class_="icon")
                if icon_p and icon_p.get_text(strip=True) == "ID" and len(p_tags) >= 2:
                    job_id = p_tags[1].get_text(strip=True)

        if not job_id:
            m = re.search(r"jobId=(\d+)", href)
            job_id = m.group(1) if m else ""
        if not job_id:
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(date_text),
            "application_url": href,
        })

    return jobs


def _fill_cache_for_keyword(keyword: str, timeout: int) -> list[dict]:
    key = keyword.strip().lower()
    if key in _cache:
        return _cache[key]

    collected: list[dict] = []
    seen_ids: set[str] = set()
    offset = 0

    for _ in range(_MAX_PAGES_PER_KEYWORD):
        url = (
            f"{_SEARCH_BASE}/{quote(keyword)}"
            f"?listFilterMode=1&jobRecordsPerPage={_SITE_PAGE_SIZE}&jobOffset={offset}"
        )
        r = _get(url, timeout)
        page_jobs = _parse_search_page(r.text)
        if not page_jobs:
            break

        new_this_page = 0
        for job in page_jobs:
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            new_this_page += 1

            loc_lower = job["location"].lower()
            if not any(tok in loc_lower for tok in _INDIA_CITY_TOKENS):
                continue  # Sydney/London/Singapore/etc. -- not an India office
            if "india" not in loc_lower:
                job["location"] = f"{job['location']}, India" if job["location"] else "India"
            collected.append(job)

        if new_this_page == 0:
            break  # pagination stalled/wrapped -- stop (UBS lesson)

        offset += _SITE_PAGE_SIZE
        time.sleep(0.15)

    _cache[key] = collected
    return collected


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
    jobs = _fill_cache_for_keyword(keyword, timeout)
    return jobs[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch job description + posting date from the plain HTML detail page.

    The same URL used as application_url serves the full description --
    no separate JSON API exists for this ATS.
    """
    r = _get(application_url, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    sections = soup.find_all("article", class_="article--details")
    description = " ".join(
        " ".join(s.get_text(separator=" ").split()) for s in sections
    ).strip()

    posting_date = ""
    meta = soup.find("article", class_="article__content__fields")
    if meta:
        for field in meta.select(".article__content__view__field"):
            label = field.select_one(".article__content__view__field__label")
            if label and label.get_text(strip=True) == "Date":
                value = field.select_one(".article__content__view__field__value")
                if value:
                    posting_date = _parse_posted_on(value.get_text(strip=True))
                break

    return description, posting_date
