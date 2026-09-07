"""
Netflix job fetcher — explore.jobs.netflix.net's own server-rendered
search (Eightfold-platform careers site; verified directly, not guessed).

Netflix's careers site (`explore.jobs.netflix.net`) runs on Eightfold AI
(same underlying ATS family as Microsoft/Qualcomm/Morgan Stanley — see
fetcher.py), but its normal public search API is disabled for this
tenant: `GET /api/pcsx/search` returns HTTP 403 `{"message": "PCSX is not
enabled for this user."}` (same wall as HSBC's Eightfold tenant — see
hsbc_fetcher.py). Unlike HSBC, no Playwright/widget workaround is needed
here: the careers page itself is genuinely server-rendered with the full,
live search JSON embedded directly in the HTML — a plain unauthenticated
`requests.get` sees real, current results with no JS execution required.
Confirmed live 2026-09-06.

    GET https://explore.jobs.netflix.net/careers?location=<location>&query=<keyword>

The embedded payload lives in a `<code id="smartApplyData" ...>` element
as HTML-entity-escaped JSON (unescape once, then `json.loads`) — keys of
interest: `positions` (the result list), `count` (total for this query),
and `query` (an echo of what was actually applied server-side, useful for
confirming the filter really took effect).

Verified via direct A/B requests against the live page:
- `location=India` genuinely narrows server-side (`count` drops from 501
  company-wide to 8) — NOT a client-side-only filter.
- `query=<keyword>` (keyword) genuinely narrows server-side (0 for a
  nonsense token) — NOT registered in `_IGNORES_KEYWORDS`.
- **No working pagination**: `start=`/`num=` query params are silently
  ignored — the page always returns the same result set regardless
  (confirmed: `?location=India&start=8&num=10` returns the identical 8
  positions as no offset at all). The unfiltered company-wide page caps
  at exactly 10 positions even though `count` reports 501, so this is a
  real "first N only" limitation of the embed, not something a page
  parameter can move past. Immaterial for India today (well under the
  cap), but `fetch_jobs` returns `[]` for any `start > 0` rather than
  re-fetching the same page pointlessly (same convention as HSBC's
  Eightfold widget).
- One result ("Director, Events & Screenings - APAC") is a genuine
  multi-region role whose *primary* `location` field alone says
  "Singapore,Singapore", but its `locations[]` array (used here, joined
  with "; ") also lists "Mumbai,Mahārāshtra,India" among 4 regions — so
  this fetcher's location string correctly retains the India signal via
  the plural field rather than only the singular one, which would have
  looked like non-India facet leakage otherwise (the class of bug already
  documented for Cisco/Walmart/other Eightfold-family tenants).

Live-verified 2026-09-06 totals: 8 raw results for India (7 single-region
Mumbai postings + the 1 multi-region APAC role above), all non-engineering
(Communications, Marketing, Finance, Production, Physical Security,
Compensation) — 0 matches against the standard keyword list, a genuine
current fact, not a fetcher defect: Netflix's India office today is a
regional business-functions hub (per these titles), not shown here to
have open India-based software engineering reqs.

Job detail: the smartApplyData embed's own `job_description` field is
always blank; the real JD lives in a `<script type="application/ld+json">`
schema.org `JobPosting` block on the same detail page (there are two
`application/ld+json` blocks on the page — a `WebSite` block and the
`JobPosting` one; only `@type == "JobPosting"` is used), which also
carries a clean ISO `datePosted`.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime, timezone

import requests

_ORIGIN = "https://explore.jobs.netflix.net"
_SEARCH_URL = f"{_ORIGIN}/careers"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_SMART_APPLY_RE = re.compile(
    r'<code id="smartApplyData"[^>]*>(.*?)</code>', re.DOTALL
)
_JSON_LD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)


class RateLimitError(Exception):
    """Raised on 429 or persistent network/parsing failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _get_with_retries(url: str, params: dict, timeout: int, label: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Netflix {label}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Netflix {label} failed: {exc}") from exc
    raise RateLimitError(f"Netflix {label}: no response — {last_exc}")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 10,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return Netflix job listings for one keyword/location combination.

    `query`/`location` are both genuine server-side filters. There is no
    working pagination on this embed (see module docstring) — `start > 0`
    always returns [] rather than re-fetching the identical page.
    """
    if start > 0:
        return []

    params: dict[str, str] = {}
    if keyword:
        params["query"] = keyword
    if location:
        params["location"] = location

    r = _get_with_retries(_SEARCH_URL, params, timeout, "search")

    m = _SMART_APPLY_RE.search(r.text)
    if not m:
        raise RateLimitError("Netflix search: smartApplyData block not found")

    try:
        data = json.loads(html_mod.unescape(m.group(1)))
    except json.JSONDecodeError as exc:
        raise RateLimitError(f"Netflix search: failed to parse smartApplyData — {exc}") from exc

    positions = data.get("positions") or []

    jobs: list[dict] = []
    for p in positions:
        job_id = str(p.get("display_job_id") or p.get("id") or "")
        title = (p.get("name") or "").strip()
        if not (job_id and title):
            continue

        locs = p.get("locations") or []
        loc = "; ".join(locs) if locs else (p.get("location") or "")

        t_create = p.get("t_create")
        posting_date = (
            datetime.fromtimestamp(t_create, tz=timezone.utc).strftime("%Y-%m-%d")
            if t_create else ""
        )

        application_url = p.get("canonicalPositionUrl") or f"{_ORIGIN}/careers/job/{job_id}"

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    return jobs[:num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date from the detail page's
    schema.org JobPosting JSON-LD block."""
    r = _get_with_retries(application_url, {}, timeout, "description")

    description = ""
    posting_date = ""
    for raw_block in _JSON_LD_RE.findall(r.text):
        try:
            d = json.loads(raw_block)
        except json.JSONDecodeError:
            continue
        if d.get("@type") != "JobPosting":
            continue
        description = _strip_html(d.get("description") or "")
        posted = (d.get("datePosted") or "").strip()
        posting_date = posted[:10] if posted else ""
        break

    return description, posting_date
