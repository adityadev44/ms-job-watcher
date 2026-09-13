"""Fetches Palo Alto Networks job listings from its own branded careers site.

jobs.paloaltonetworks.com is NOT a third-party ATS subdomain -- DevTools
inspection (2026-09-13) shows a fully server-rendered HTML site (Radancy/
TalentBrew -- confirmed via `tbcdn.talentbrew.com`/`radancy` strings in the
page source, same vendor family already documented for Boeing/Disney/
Unilever elsewhere in this repo, here using the "section29"/"section30"
CSS-class theme rather than those companies' "search-results__job-*" theme).
No client-side JS/XHR is needed at all:

    GET https://jobs.paloaltonetworks.com/en/location/india-jobs/47263/1269750/2/{page}

-- returns page {page} (30 results/page) of the India location facet
(org id 47263, location id 1269750, radius code 2). Confirmed live: 9 pages,
~264 total India postings. The real ATS behind "Apply" is Workday
(`paloaltonetworks.wd5.myworkdayjobs.com/panwexternalcareers/...`), but the
public search/listing surface never touches that API -- this fetcher only
needs the branded site.

Location facet is India-only already (all results say "..., India" or a
bare 6-digit pincode + ", India") -- no client-side leakage risk observed.
Keyword search exists at a separate path (`/en/search-jobs/{keyword}/47263/
{page}`) but returns the GLOBAL pool, not India-scoped (confirmed: mixes in
US/UK/Israel results) -- so keyword search is not used here. Same
"cache-once, ignore keywords" family as MongoDB/Genpact/WTW: the location
listing is fetched once, cached in-module, keyword/location args accepted
for interface compatibility only.

Detail pages embed a full schema.org JobPosting JSON-LD block
(`datePosted`, `description` as raw HTML) -- no separate API call needed,
just parse the `<script type="application/ld+json">` tag.
"""
from __future__ import annotations

import html as _html_mod
import json
import re
import time

import requests

_ORG_ID = "47263"
_INDIA_LOCATION_PATH = f"https://jobs.paloaltonetworks.com/en/location/india-jobs/{_ORG_ID}/1269750/2"
_MAX_PAGES = 25  # defensive cap -- observed ~10 pages of ~15 unique jobs each

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

_JOB_ROW_RE = re.compile(
    r'class="section29__search-results-link" href="(?P<href>[^"]+)" data-job-id="(?P<id>\d+)">'
    r'\s*<h2 class="section29__search-results-job-title">(?P<title>[^<]*)</h2>'
    r'.*?result-location newLoc">(?P<loc>[^<]*)</span>',
    re.S,
)

_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from the PANW career site."""


def _get_with_retry(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"PANW {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"PANW {what} failed: {exc}") from exc
    raise RateLimitError(f"PANW {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        url = f"{_INDIA_LOCATION_PATH}/{page}"
        r = _get_with_retry(url, timeout, f"page {page}")
        rows = list(_JOB_ROW_RE.finditer(r.text))
        if not rows:
            break
        for m in rows:
            job_id = m.group("id")
            title = _html_mod.unescape(m.group("title")).strip()
            loc = _html_mod.unescape(m.group("loc")).strip()
            href = m.group("href")
            if not (job_id and title):
                continue
            if "india" not in loc.lower():
                continue
            app_url = f"https://jobs.paloaltonetworks.com{href}" if href.startswith("/") else href
            collected.append({
                "id": job_id,
                "title": title,
                "location": loc,
                "posting_date": "",
                "application_url": app_url,
            })

    _india_cache[:] = collected
    print(f"[PaloAltoNetworks] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of PANW India jobs.

    The branded career site's location listing has no server-side keyword
    filter (its separate keyword-search path returns the global, non-India
    pool instead), so the India-scoped listing is fetched once and cached;
    keyword/location arguments are accepted for interface compatibility.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) parsed from the detail page's
    embedded schema.org JobPosting JSON-LD block."""
    r = _get_with_retry(application_url, timeout, "description fetch")
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S)
    if not m:
        raise RateLimitError(f"PANW description: no JSON-LD found at {application_url!r}")

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise RateLimitError(f"PANW description: bad JSON-LD at {application_url!r}: {exc}") from exc

    raw_html = data.get("description") or ""
    text = _html_mod.unescape(re.sub(r"<[^>]+>", " ", raw_html))
    description = " ".join(text.split())

    raw_date = data.get("datePosted") or ""
    posting_date = ""
    dm = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw_date)
    if dm:
        posting_date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

    return description, posting_date
