"""Fetch Airbnb jobs from its official WordPress + FacetWP careers site.

Live verified 2026-09-13. `careers.airbnb.com/positions/` is WordPress with
the FacetWP plugin (`FWP_JSON` in the page source, `where_you_work` facet
option `"india"`). The plugin's own front-end JS (`front.min.js`) resolves to
`FWP.template === "wp"`, which means the AJAX refresh POSTs
`{"action":"facetwp_refresh","data":{...}}` as JSON directly back to the
listing page URL (`https://careers.airbnb.com/positions/`), not to the
`/wp-json/facetwp/v1/refresh` REST route that a naive read of `FWP_JSON`
would suggest (that route returned an empty `[]` for every payload tried).
The response's `template` field is the *entire* re-rendered page HTML; job
rows live in `ul.job-list li`. India has a small, stable pool (8 open roles
at verification time; 17 total pages / 164 jobs company-wide), so the fetcher
caches all India rows once and ignores the keyword parameter.

There is no genuine posting-date field: the detail page's JSON-LD
`datePublished` is the render/crawl time (confirmed by fetching three
different job IDs — all returned an identical timestamp of "now"), not the
job's real posting date. `posting_date` is left empty, same convention as
`progresssoftware_fetcher.py`.
"""
from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

_LIST_URL = "https://careers.airbnb.com/positions/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
}
_PER_PAGE = 10
_cache: list[dict] | None = None


class RateLimitError(Exception):
    pass


def _post(page: int, timeout: int) -> dict:
    body = {
        "action": "facetwp_refresh",
        "data": {
            "facets": {
                "search_input": "",
                "departments": [],
                "where_you_work": ["india"],
                "workplace_type": [],
            },
            "frozen_facets": {},
            "http_params": {"get": {}, "uri": "positions", "url_vars": []},
            "template": "wp",
            "extras": {
                "jobs_total": True,
                "jobs_pager": True,
                "jobs_sort": True,
                "jobs_pagination": True,
            },
            "soft_refresh": 0,
            "is_bfcache": 0,
            "first_load": 0,
            "paged": page,
        },
    }
    for attempt in range(3):
        try:
            r = requests.post(_LIST_URL, json=body, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("Airbnb careers rate limited")
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or "template" not in data:
                raise RateLimitError("Airbnb careers returned an unexpected payload")
            return data
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Airbnb careers fetch failed: {exc}") from exc
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def _parse_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("ul.job-list li"):
        a = li.select_one("a[href*='/positions/']")
        if not a:
            continue
        m = re.search(r"/positions/(\d+)/?", a.get("href", ""))
        if not m:
            continue
        jid = m.group(1)
        title = a.get_text(" ", strip=True)
        loc_span = li.select_one("div.col-span-4.lg\\:col-span-3 span")
        location = loc_span.get_text(" ", strip=True) if loc_span else ""
        out.append(
            {
                "id": jid,
                "title": title,
                "location": location,
                "posting_date": "",
                "application_url": f"https://careers.airbnb.com/positions/{jid}/",
            }
        )
    return out


def _fill_cache(timeout: int) -> list[dict]:
    global _cache
    if _cache is not None:
        return _cache
    first = _post(1, timeout)
    rows = _parse_rows(first["template"])
    total_pages = first.get("settings", {}).get("pager", {}).get("total_pages", 1)
    for page in range(2, total_pages + 1):
        rows.extend(_parse_rows(_post(page, timeout)["template"]))
    _cache = list({r["id"]: r for r in rows}.values())
    return _cache


def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    return _fill_cache(timeout)[start : start + num]


def fetch_job_description(application_url, timeout=20):
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("Airbnb careers rate limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Airbnb job detail fetch failed: {exc}") from exc
            time.sleep(2**attempt)
    soup = BeautifulSoup(r.text, "html.parser")
    panel = soup.select_one("#job-detail-panel")
    text = panel.get_text(" ", strip=True) if panel else ""
    return (text, "")
