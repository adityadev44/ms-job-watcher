"""Fetches Electronic Arts (EA) job listings via jobs.ea.com (Avature ATS).

EA's careers site (`https://www.ea.com/careers` -> `https://jobs.ea.com`)
runs on Avature — same ATS vendor as Macquarie Group
(`recruitment.macquarie.com`, see macquarie_fetcher.py), but a newer
Avature theme (page HTML embeds `avature.wizard.registrars` JS globals and
a `portalpacks/web/js/wizard-legacy...` bundle instead of Macquarie's
plainer classic skin). Despite the different theme, the underlying
`SearchJobs` HTML endpoint used by Macquarie works identically here —
confirmed live, no Playwright needed:

    GET https://jobs.ea.com/en_US/careers/SearchJobs/{urlencoded keyword}
        ?listFilterMode=1&jobRecordsPerPage=9&jobOffset={N}

Verified live 2026-09-14:
- Plain `requests.get` (no session/cookie dance) returns full server-rendered
  HTML with `<article class="article--result">` blocks — same tag as
  Macquarie, but a different inner layout: title/location/id/worker-type/
  department all live in one `.article__header__text__subtitle` line of
  spans (`.list-item-location`, `.list-item-id` "Role ID N",
  `.list-item-workerType`, `.list-item-department`), not Macquarie's
  `.article__details__data` icon-labelled divs. No date field is exposed
  anywhere on this theme (search results or detail page) — `posting_date`
  is left empty, same acceptable gap as IBM ("no posting-date field exposed
  anywhere").
- EA has a real, substantial Hyderabad studio: `search=india` and
  `search=software engineer hyderabad` both return **exclusively**
  "Hyderabad, India" postings (verified: `search=pune` returns 0 results,
  so no Pune-only concern the way Ubisoft/Icertis have). Titles include
  "Software Engineer III", "SSE AI", "Data Engineer II", "Full Stack
  Software Engineer Intern" — genuine SDE roles, not just QA/ops.
- Keyword search genuinely narrows results server-side (site-observed
  behavior consistent with Macquarie's Avature classic engine) — each
  keyword's full India-filtered result set is fetched once and cached,
  same pattern as macquarie_fetcher.py.
- Job IDs come from the "Role ID N" label in the result card, not the
  detail URL's trailing path segment (EA's detail URLs are
  `/JobDetail/{slug}/{id}`, same id, but the explicit "Role ID" label is
  more robust than a regex against a human-readable slug that can contain
  digits).
- Job-detail pages are plain server-rendered HTML: the same
  `article--details` class Macquarie uses holds "General Information"
  (locations/Role ID/Worker Type/Studio/Work Model) as one block and
  "Description & Requirements" as a second block — the real JD text is in
  the *second* `article--details` block, not the first. No separate
  `article__content__fields` date block exists on this theme (see above).
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_BASE = "https://jobs.ea.com"
_SEARCH_BASE = f"{_BASE}/en_US/careers/SearchJobs"

_SITE_PAGE_SIZE = 9  # observed fixed page size, same as Macquarie's Avature classic theme
_MAX_PAGES_PER_KEYWORD = 60  # defensive cap (~540 raw jobs) against runaway pagination

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
    """Raised on 429 / persistent connection failure from EA's site."""


# Module-level cache: filled once per keyword, reused across matcher.py's
# repeated (start, num) page calls for that same keyword.
_cache: dict[str, list[dict]] = {}


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("EA: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"EA request failed: {exc}") from exc
    raise RateLimitError(f"EA request failed: {last_exc}")


def _parse_search_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []

    for article in soup.find_all("article", class_="article--result"):
        link = article.select_one(".article__header__text__title a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not (title and href):
            continue

        loc_span = article.select_one(".list-item-location")
        location = loc_span.get_text(strip=True) if loc_span else ""

        job_id = ""
        id_span = article.select_one(".list-item-id")
        if id_span:
            m = re.search(r"(\d+)", id_span.get_text(strip=True))
            if m:
                job_id = m.group(1)
        if not job_id:
            m = re.search(r"/JobDetail/[^/]+/(\d+)", href)
            job_id = m.group(1) if m else ""
        if not job_id:
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",  # no date field exposed anywhere on this theme
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

            if "india" not in job["location"].lower():
                continue  # not an India posting
            collected.append(job)

        if new_this_page == 0:
            break  # pagination stalled/wrapped

        offset += _SITE_PAGE_SIZE
        time.sleep(0.15)

    _cache[key] = collected
    return collected


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
    """Fetch job description from the plain HTML detail page.

    No posting-date field is exposed on the detail page for this Avature
    theme either, so the second return value is always "".
    """
    r = _get(application_url, timeout)
    soup = BeautifulSoup(r.text, "html.parser")

    sections = soup.find_all("article", class_="article--details")
    # The "Description & Requirements" content sits in the second block;
    # the first is the "General Information" summary table. Join both to
    # be resilient to layout drift, but the real JD text always comes from
    # position 1 (0-indexed) when both are present.
    description = " ".join(
        " ".join(s.get_text(separator=" ").split()) for s in sections
    ).strip()

    return description, ""
