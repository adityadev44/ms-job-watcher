"""
GE HealthCare (India GCC) job fetcher — Phenom People (careers.gehealthcare.com).

Pre-onboarding recon guessed "Oracle HCM CE" from the URL shape alone
(same family as Chubb/Amex/Icertis/Hexaware/eClerx). That guess was WRONG —
verified from scratch via a live response: `careers.gehealthcare.com` sets
`PLAY_SESSION` / `PHPPPE_ACT` cookies and loads `cdn.phenompeople.com` /
`assets.phenompeople.com` assets, the same Phenom People signature already
seen in this repo at Morningstar and United Airlines (India Knowledge
Center). This is the third confirmed case in this repo's history of a
URL-shape-only ATS guess turning out wrong (see PLAYBOOK.md's Wave 5 entry
on Boeing/PepsiCo/Walmart/United Airlines).

Unlike Morningstar (whose `/widgets` API needs a browser-JS session and had
to fall back to sitemap+JSON-LD scraping), this tenant's search-results page
embeds the full search response server-side as a `phApp.ddo = {...}` JSON
blob in the plain HTML — the same easy pattern already used for United
Airlines' dedicated India landing page. No Playwright needed anywhere in
this fetcher; plain `requests` reads both the search page and (see below)
the job-description endpoint.

Search: `GET /global/en/search-results?keywords=...&from=...`
  - `keywords` is genuinely applied server-side (different keywords return
    different `totalHits`: 979 with no keyword, 426 for "software engineer",
    58 for "python developer", 0 for a nonsense string).
  - `from` is a genuine, CLEAN offset — confirmed no wraparound (unlike the
    UBS/MUFG/Nvidia/Pfizer/Walmart family of bugs in this repo): requesting
    `from=` past the real total returns an empty `jobs` list, HTTP 200, and
    stays empty on every further page.
  - Page size is fixed at 10 server-side; a `num`/`size`-style override was
    tried and had no effect.
  - No location facet actually narrows results server-side — `location=India`
    returns 0 hits (probably needs a geocoded place_id, not a bare string)
    and `country=India` is silently ignored (global results still come back).
    Same "fetch globally, filter India client-side" shape as WTW/Icertis/
    Hexaware/eClerx/Fiserv/Genpact in this repo. Each job's own `country`
    field is a clean, always-populated exact value ("India"), so no
    Icertis/Fiserv-style "does the location text even say India" ambiguity.

Because `keywords` genuinely narrows the pool per call (unlike PepsiCo/UBS/
Deutsche Bank, where the whole point of caching is that keywords are a
no-op), and because raw pagination is clean with no wraparound, this
fetcher caches the *complete* India-filtered result set per keyword on the
first call for that keyword (walking every real page via `from=`, not just
one), then serves every subsequent `fetch_jobs()` call for that keyword as a
local slice. This sidesteps a real, if subtle, correctness trap: matcher.py
advances its own pagination cursor by `len(page)` (the FILTERED count
returned), not by the raw page size — a per-page-filter fetcher (the
Icertis/Fiserv style) that filters India *before* returning would silently
feed that filtered count back in as the next raw `from=` offset, causing
severe redundant overlap or (worse) early termination the moment one raw
page of 10 happens to contain zero India jobs. Caching the whole pool once
avoids ever hitting that trap. Verified per-keyword pool sizes stay small
(largest observed: 534 raw / 96 India for "senior software engineer"), so
one full walk costs at most ~55 requests — comparable to or cheaper than
several already-registered companies in this repo.

Full job descriptions are NOT inline in the search response — only a
truncated `descriptionTeaser` (~300 chars) is. But each job's own `applyUrl`
already resolves to a live Workday page
(`gehc.wd5.myworkdayjobs.com/GEHC_ExternalSite/job/.../apply`) — GE
HealthCare's real hiring backend is Workday even though the public search
frontend is Phenom People, the same "Phenom/TalentBrew frontend skin over a
Workday-or-similar backend" shape already seen at Boeing (TalentBrew over
Workday) and Barclays (TalentBrew skin over Workday CXS). The Workday CXS
detail API is reachable directly with plain `requests` (confirmed: strip the
trailing `/apply` segment, insert `/wday/cxs/{tenant}/{site}` before the job
path) and returns the full HTML job description with no auth needed — same
family as Wells Fargo / Deutsche Bank's Workday-CXS-for-descriptions pattern.
`applyUrl` is used directly as `application_url` since it is itself the
correct, real, clickable apply link.

The Workday detail response's own `postedOn` field is a coarse relative
string ("Posted 30+ Days Ago"), not a real date — but the Phenom search
response already carries a clean ISO `postedDate` per job, so `fetch_jobs()`
uses that directly and `fetch_job_description()` returns "" for its date
half of the tuple (matcher.py only overwrites `job["posting_date"]` when the
returned date is truthy, so the already-good date from the search response
is left alone).
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import requests

_BASE_URL = "https://careers.gehealthcare.com"
_SEARCH_URL = f"{_BASE_URL}/global/en/search-results"
_DDO_MARKER = "phApp.ddo = {"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

_WD_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/json",
}

_MAX_RAW_PAGES = 120  # safety ceiling (~1200 raw jobs; largest observed pool is 979)
_PAGE_DELAY = 0.15    # politeness delay between our own internal raw-page requests

# keyword -> fully-walked, India-filtered job dicts (see module docstring for
# why this must cache the *entire* pool per keyword rather than filter
# page-by-page).
_keyword_cache: dict[str, list[dict]] = {}


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _extract_ddo(html: str) -> dict | None:
    """Pull the `phApp.ddo = {...}` JSON blob out of the server-rendered page.

    A plain regex can't safely find the matching closing brace (the blob
    contains nested objects and job description text with its own braces/
    quotes), so this walks the string tracking string/escape state and brace
    depth, same technique verified during recon.
    """
    idx = html.find(_DDO_MARKER)
    if idx == -1:
        return None
    start = idx + len(_DDO_MARKER) - 1  # position of the opening '{'
    depth = 0
    in_str = False
    esc = False
    quote = None
    i = start
    n = len(html)
    while i < n:
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        else:
            if c in "\"'":
                in_str = True
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1
    blob = html[start:i]
    import json as json_mod
    try:
        return json_mod.loads(blob)
    except ValueError:
        return None


def _fetch_raw_page(keyword: str, from_: int, timeout: int) -> list[dict]:
    params = {"keywords": keyword, "from": from_}
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"GE HealthCare search: 429 rate-limited (keyword={keyword!r})")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"GE HealthCare search failed (keyword={keyword!r}): {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"GE HealthCare search: no response — {last_exc}")

    data = _extract_ddo(r.text)
    if not data:
        return []
    erf = data.get("eagerLoadRefineSearch", {}) or {}
    return erf.get("data", {}).get("jobs", []) or []


def _search(keyword: str, timeout: int) -> list[dict]:
    """Walk every real page for *keyword* and return parsed India job dicts."""
    raw_jobs: list[dict] = []
    from_ = 0
    for page_num in range(_MAX_RAW_PAGES):
        if page_num > 0:
            time.sleep(_PAGE_DELAY)
        batch = _fetch_raw_page(keyword, from_, timeout)
        if not batch:
            break
        raw_jobs.extend(batch)
        from_ += len(batch)

    jobs: list[dict] = []
    for j in raw_jobs:
        country = (j.get("country") or "").strip()
        if country.lower() != "india":
            continue
        job_id = str(j.get("jobId") or j.get("reqId") or "")
        apply_url = j.get("applyUrl") or ""
        if not job_id or not apply_url:
            continue
        location = (
            j.get("cityStateCountry")
            or j.get("location")
            or f"{j.get('city', '')}, {country}".strip(", ")
        )
        posting_date = (j.get("postedDate") or j.get("dateCreated") or "")[:10]
        jobs.append({
            "id": job_id,
            "title": (j.get("title") or "").strip(),
            "location": location,
            "posting_date": posting_date,
            "application_url": apply_url,
        })
    return jobs


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of GE HealthCare's India job results for *keyword*.

    `location` is accepted for interface compatibility but ignored — GE
    HealthCare's own `location=`/`country=` query params do not reliably
    filter server-side (see module docstring), so every job's own `country`
    field is checked instead, once, when the keyword's full pool is cached.
    """
    key = keyword or ""
    if key not in _keyword_cache:
        _keyword_cache[key] = _search(key, timeout)
    return _keyword_cache[key][start:start + num]


def _cxs_url_from_apply_url(apply_url: str) -> str | None:
    """Derive the Workday CXS job-detail API URL from a Phenom `applyUrl`.

    `applyUrl` looks like:
      https://gehc.wd5.myworkdayjobs.com/GEHC_ExternalSite/job/<path>/apply
    The CXS detail API for the same posting is:
      https://gehc.wd5.myworkdayjobs.com/wday/cxs/gehc/GEHC_ExternalSite/job/<path>
    """
    parsed = urlparse(apply_url)
    host = parsed.netloc
    if not host.endswith(".myworkdayjobs.com"):
        return None
    tenant = host.split(".", 1)[0]
    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[-1].lower() == "apply":
        segments = segments[:-1]
    if len(segments) < 2:
        return None
    site = segments[0]
    job_path = "/" + "/".join(segments[1:])
    return f"https://{host}/wday/cxs/{tenant}/{site}{job_path}"


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    cxs_url = _cxs_url_from_apply_url(application_url)
    if not cxs_url:
        return "", ""

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(cxs_url, headers=_WD_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"GE HealthCare description: 429 for {application_url}")
            if r.status_code == 404:
                # Posting closed/removed since it was listed in search.
                return "", ""
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"GE HealthCare description fetch failed for {application_url}: {exc}"
            ) from exc

    if r is None:
        raise RateLimitError(f"GE HealthCare description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        return "", ""

    jpi = data.get("jobPostingInfo", {}) or {}
    description = _strip_html(jpi.get("jobDescription", "") or "")
    return description, ""
