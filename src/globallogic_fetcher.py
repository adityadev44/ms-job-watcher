"""Fetches GlobalLogic India job listings via their WordPress career site.

GlobalLogic's job search moved from /in/careers/?location=india to a
dedicated search page at /in/career-search-page/?location=india (discovered
via the AJAX filter's redirect_url response, confirmed 2026-09-06).

Key facts confirmed live 2026-09-06:
  - 143 India jobs across 15 pages of 10.
  - URL pattern: https://www.globallogic.com/in/career-search-page/?location=india
    for page 1; .../page/{N}/?location=india for subsequent pages.
  - Keyword filter: GET param `keywords=<term>` is applied server-side
    (confirmed: "software engineer" → 5 results, nonsense → empty result
    area). Per-keyword pagination is used accordingly.
  - Each job card is `<a class="job_box">` with `<h4>` title (includes IRC
    code, e.g. "Boomi Developer IRC304007"), and `<span class="job_location">`
    location spans (country + city, sometimes reversed).
  - Job ID is the IRC code extracted from the detail page URL slug:
    e.g. "irc304007" from /in/careers/boomi-developer-irc304007/
  - No posting date in the listing; detail page `div.career_banner_sub_head`
    has "Published on D Month YYYY" (e.g. "Published on 4 September 2026").
  - Description: `div.career_detail_area` on the individual job page.

Wraparound guard: /page/{N}/ past the last page returns 404 or an empty
result area — the loop breaks on either condition.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://www.globallogic.com"
_SEARCH_URL = f"{_BASE_URL}/in/career-search-page/"
_PAGE_SIZE = 10  # observed fixed by the WP theme

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.globallogic.com/in/career-search-page/",
}

_IRC_RE = re.compile(r"-(irc\d+)/?$", re.I)


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from GlobalLogic."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_detail_date(raw: str) -> str:
    """'Published on 4 September 2026' → '2026-09-04'."""
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw or "")
    if not m:
        return ""
    try:
        return datetime.strptime(
            f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _get_page_url(page: int) -> str:
    if page == 1:
        return _SEARCH_URL
    return f"{_SEARCH_URL}page/{page}/"


def _fetch_html(url: str, params: dict, timeout: int) -> str | None:
    """GET one page; returns None on 404 (end of pagination)."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("GlobalLogic: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"GlobalLogic fetch failed: {exc}") from exc

    raise RateLimitError(f"GlobalLogic: no response — {last_exc}")


# ---------------------------------------------------------------------------
# Public API
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
    """Return a page of GlobalLogic India jobs matching *keyword*.

    Pagination maps start/num → page numbers (10 jobs per WP page).
    """
    # Which WP pages cover [start, start+num)?
    first_wp_page = start // _PAGE_SIZE + 1
    last_wp_page = (start + num - 1) // _PAGE_SIZE + 1

    params: dict[str, str] = {"location": "india"}
    if keyword:
        params["keywords"] = keyword

    collected: list[dict] = []
    seen_ids: set[str] = set()

    for wp_page in range(first_wp_page, last_wp_page + 1):
        url = _get_page_url(wp_page)
        html_text = _fetch_html(url, params, timeout)
        if html_text is None:
            break  # 404 → past last page

        soup = BeautifulSoup(html_text, "html.parser")
        result_area = soup.find(class_="career_filter_result")
        if not result_area:
            break  # no result area → keyword returned no results

        boxes = result_area.find_all("a", class_="job_box")
        if not boxes:
            break

        for box in boxes:
            href = box.get("href", "").strip().rstrip("/")
            if not href:
                continue

            # IRC code from URL slug
            irc_m = _IRC_RE.search(href)
            job_id = irc_m.group(1).lower() if irc_m else ""
            if not job_id or job_id in seen_ids:
                continue

            # Title from h4 (includes IRC code — strip it)
            h4 = box.find("h4")
            raw_title = h4.get_text(strip=True) if h4 else ""
            title = re.sub(r"\s+IRC\d+$", "", raw_title, flags=re.I).strip()
            if not title:
                continue

            # Location: collect all job_location spans, skip bare "India"
            spans = box.find_all("span", class_="job_location")
            cities = [s.get_text(strip=True) for s in spans
                      if s.get_text(strip=True).lower() != "india"]
            if cities:
                location_str = f"{', '.join(cities)}, India"
            else:
                location_str = "India"

            seen_ids.add(job_id)
            collected.append({
                "id": job_id,
                "title": title,
                "location": location_str,
                "posting_date": "",
                "application_url": href if href.startswith("http") else f"{_BASE_URL}{href}",
            })

        if len(boxes) < _PAGE_SIZE:
            break  # partial page → last page reached
        if wp_page < last_wp_page:
            time.sleep(0.2)

    # Slice to the requested window
    offset_in_first_page = start - (first_wp_page - 1) * _PAGE_SIZE
    return collected[offset_in_first_page : offset_in_first_page + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for one GlobalLogic job.

    Detail page uses `div.career_detail_area` for the JD body and
    `div.career_banner_sub_head` for the "Published on D Month YYYY" date.
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
                raise RateLimitError(f"GlobalLogic description: 429 for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"GlobalLogic description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"GlobalLogic description: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")

    desc_div = soup.find(class_="career_detail_area")
    description = ""
    if desc_div:
        raw = html_mod.unescape(desc_div.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    banner_sub = soup.find(class_="career_banner_sub_head")
    if banner_sub:
        posting_date = _parse_detail_date(banner_sub.get_text(strip=True))

    return description, posting_date
