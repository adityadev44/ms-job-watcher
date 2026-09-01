"""Fetches DataRobot job listings via its WordPress career-page AJAX endpoint.

DataRobot's careers page (datarobot.com/careers/open-positions/) is a custom
WordPress theme, NOT a third-party ATS subdomain -- no Greenhouse/Lever/
Ashby/Workday board exists for this company (all four probed and 404/empty).
The page markup borrows Algolia InstantSearch's CSS class names
("ais-RefinementList-item" etc.) but no actual Algolia calls happen; a real
network capture (Playwright, requests both attempted) shows pagination fires
a plain WordPress AJAX POST instead:

    POST https://www.datarobot.com/wp-admin/admin-ajax.php
    Content-Type: multipart/form-data
    action=ajax_careers_search&nonce={nonce}&page={n}&per_page={n}
      &filters[locations][]={slug}   (optional, repeatable)
      &filters[department][]={dept}  (optional, repeatable)

Verified live 2026-08-31 via plain `requests` (no Playwright/session cookie
needed) -- HTTP 200, real HTML-fragment job cards embedded in
`{"success":true,"data":{"html": "...", "total": N, "pages": N}}`.

The `nonce` is a WordPress security token embedded in the initial page load
as `drAjax = {"ajaxurl":"...","nonce":"..."}` -- scraped via regex from a
plain GET of the listing page before the first AJAX call. It is NOT tied to
a session cookie in practice (confirmed: a fresh curl with no prior cookies
against admin-ajax.php using a nonce scraped from a separate, unauthenticated
GET succeeds) but WordPress nonces do expire (~12-24h typical), so it is
re-scraped once per module cache-fill rather than hardcoded.

Small company / small board: only 16 total open positions worldwide as of
2026-08-31 (`per_page=50` returns the whole board in a single call --
`pages` comes back as 1). **Zero of the 16 are in India right now** -- the
site's own location-filter checkbox list (46 values) contains ONLY North
America/EMEA/APAC-excluding-India remote/office labels (Boston, Houston, SF,
Seattle, Kyiv, Lviv, Remote Poland/Ukraine/Saudi Arabia/Dubai/Japan/Canada/
US-states) -- no Bangalore/Hyderabad/Pune/India-remote option exists at all.
This is a genuine current fact (small, US/EMEA-centric company today), not a
fetcher defect -- same "confirmed zero" class as ING/AIG/DataRobot-peer
Nasdaq elsewhere in this repo. Mechanics were verified against the real
global pool (Boston/Houston/Remote-Dubai/Remote-Saudi-Arabia postings all
fetch correctly); this fetcher will correctly pick up an India posting the
day DataRobot opens one, with no code change needed.

Job-detail pages (`/careers/open-positions/job/{id}/`) are plain
server-rendered HTML (no Playwright anywhere in this pipeline) carrying a
clean schema.org JobPosting JSON-LD block. `datePosted` is NOT ISO-8601 here
-- it's the literal string "Posting Date: MM/DD/YYYY" -- parsed with a
regex rather than assumed pre-formatted (a new date-format quirk for this
repo, worth flagging if another WordPress-careers-theme company turns up).

Titles carry real signal ("Senior AI Engineer - Professional Services",
"Lead AI Engineer", "Staff Software Engineer") -- direct AI-platform product
company, not an IT-services generic-title shop. require_tech_in_description
is NOT enabled.
"""
from __future__ import annotations

import re
import time

import requests

_ORIGIN = "https://www.datarobot.com"
_LIST_PAGE_URL = f"{_ORIGIN}/careers/open-positions/"
_AJAX_URL = f"{_ORIGIN}/wp-admin/admin-ajax.php"
_DETAIL_URL_TMPL = f"{_ORIGIN}/careers/open-positions/job/{{job_id}}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json",
    "Referer": _LIST_PAGE_URL,
}

_NONCE_RE = re.compile(r'drAjax\s*=\s*\{[^}]*"nonce"\s*:\s*"([a-f0-9]+)"')
_CARD_RE = re.compile(
    r'href="(/careers/open-positions/job/\d+)"\s*>.*?'
    r'class="department[^"]*"[^>]*title="([^"]*)"\s*>.*?'
    r'class="uk-card-title role[^"]*"[^>]*title="([^"]*)"[^>]*>.*?'
    r'class="location[^"]*">\s*<span[^>]*title="([^"]*)"',
    re.DOTALL,
)
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

_jobs_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure."""


def _get_with_retry(url: str, timeout: int, what: str, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout, **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"DataRobot {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"DataRobot {what} failed: {exc}") from exc
    raise RateLimitError(f"DataRobot {what}: no response -- {last_exc}")


def _post_with_retry(data: dict, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(_AJAX_URL, headers=_HEADERS, data=data, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"DataRobot {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"DataRobot {what} failed: {exc}") from exc
    raise RateLimitError(f"DataRobot {what}: no response -- {last_exc}")


def _fetch_nonce(timeout: int) -> str:
    r = _get_with_retry(_LIST_PAGE_URL, timeout, "nonce fetch")
    m = _NONCE_RE.search(r.text)
    if not m:
        raise RateLimitError("DataRobot: could not find drAjax nonce on listing page")
    return m.group(1)


def _parse_cards(html_fragment: str) -> list[dict]:
    out = []
    for href, _dept, title, loc in _CARD_RE.findall(html_fragment):
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        out.append({"id": job_id, "title": title.strip(), "location": loc.strip()})
    return out


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire DataRobot board once; cache job list.

    _cache_filled is set before the request attempt so a transient failure
    doesn't retry on every fetch_jobs() call in the same process run.
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    nonce = _fetch_nonce(timeout)
    r = _post_with_retry(
        {"action": "ajax_careers_search", "nonce": nonce, "page": 1, "per_page": 100},
        timeout,
        "cache fill",
    )
    payload = r.json()
    if not payload.get("success"):
        raise RateLimitError(f"DataRobot cache fill: unexpected payload {payload!r}")

    data = payload.get("data", {})
    cards = _parse_cards(data.get("html", "") or "")

    # If the board ever exceeds one page (unlikely at 16 jobs, but the site's
    # own per_page default is 9), walk remaining pages defensively.
    total_pages = int(data.get("pages") or 1)
    for pg in range(2, total_pages + 1):
        r = _post_with_retry(
            {"action": "ajax_careers_search", "nonce": nonce, "page": pg, "per_page": 100},
            timeout,
            f"cache fill page {pg}",
        )
        p2 = r.json()
        if p2.get("success"):
            cards.extend(_parse_cards(p2.get("data", {}).get("html", "") or ""))

    collected: list[dict] = []
    for c in cards:
        if "india" not in c["location"].lower():
            continue
        collected.append({
            "id": c["id"],
            "title": c["title"],
            "location": c["location"],
            "posting_date": "",  # filled in on description fetch
            "application_url": _DETAIL_URL_TMPL.format(job_id=c["id"]),
        })

    _jobs_cache[:] = collected
    print(f"[DataRobot] Cache filled: {len(collected)} India jobs (of {len(cards)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of DataRobot India jobs.

    keyword/location are accepted but ignored -- the site's own AJAX search
    supports filters, but the whole (currently small) board is cached once
    and filtered client-side, same pattern as other small-board fetchers in
    this repo. The shared matcher does the real title/skill filtering.
    """
    _fill_cache(timeout=timeout)
    return _jobs_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) parsed from the detail page's
    schema.org JobPosting JSON-LD block.
    """
    r = _get_with_retry(application_url, timeout, "description fetch")
    html = r.text

    m = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return "", ""

    import json as _json
    try:
        data = _json.loads(m.group(1))
    except ValueError:
        return "", ""

    desc_html = data.get("description", "") or ""
    import html as _html_mod
    text = re.sub(r"<[^>]+>", " ", _html_mod.unescape(desc_html))
    text = " ".join(text.split())

    posted = ""
    dm = _DATE_RE.search(data.get("datePosted", "") or "")
    if dm:
        month, day, year = dm.groups()
        posted = f"{year}-{int(month):02d}-{int(day):02d}"

    return text, posted
