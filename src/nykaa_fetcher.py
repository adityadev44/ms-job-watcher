"""Fetches Nykaa job listings from their official careers.nykaa.com site.

ATS discovery: Nykaa's advertised careers page (careers.nykaa.com) is a
server-rendered Astro app hosted on Cloudflare Pages (`skima-careers-
frontend.pages.dev`), for an ATS/career-page-builder product called
**"Skima AI"** (skima.ai, Mumbai-based; not one of the vendors in the
PLAYBOOK's Common ATS table -- not Workday, SuccessFactors, Oracle HCM CE,
Greenhouse, Lever, iCIMS, Taleo, Darwinbox, SmartRecruiters, RippleHire,
Avature, or Lenskart's "ainterviews.com"). Confirmed live 2026-08-31:
  - `<link rel="canonical" href="https://careers.nykaa.com.skima.ai">` and a
    Skima-owned Cloudflare Pages script tag are the fingerprint for this ATS.
  - The company's listing page (`GET https://careers.nykaa.com/`) is fully
    server-rendered HTML -- no client-side JSON API call is made for the job
    list itself (the page's own small JS bundle only reflects the search box
    into `?by_job_name=` and reloads the page; the actual data-fetch happens
    server-side before the SSR response is returned). There is no separate
    public JSON API to call instead -- this is a plain HTML-scraping
    integration, same family as Siemens/Macquarie/SAP Labs.
  - Job-detail pages are server-rendered at a **flat** path directly under
    the domain root: `https://careers.nykaa.com/{job-uuid}` (confirmed
    against 3 real, Google-indexed job URLs -- no `/job/` or `/jobs/`
    prefix, unlike most other ATSes in this repo).

**Confirmed-zero current state (verified, not a fetcher bug):** as of
2026-08-31, `careers.nykaa.com/` renders "Currently there are no job
postings available." and `careers.nykaa.com/sitemap.xml` is an empty
`<urlset>`. This was cross-checked against 3 real job URLs Google had
indexed for this domain (all "Nykaa Fashion" roles, e.g.
`.../54a30b39-e12b-4df4-9496-e4e56729eb8d`) -- every one of them renders its
full detail page (title/description/apply button all present) but with the
apply button disabled and "This job posting is no longer accepting
applications" -- i.e. Google's cache is simply stale from when these roles
were open; they have since closed, and nothing has been reposted through
this specific portal since. Nykaa clearly does hire software engineers
right now (LinkedIn independently shows a live "Software Engineer in Test"
req in Gurugram as of 2026-08-31), but not through this ATS instance at this
moment -- same class of situation as ING (0/734 India) or eClerx (0 titles
pass title-family) already documented in the PLAYBOOK: a mechanically
correct, verified-working fetcher hitting a genuine current zero. This
self-heals automatically the next time Nykaa posts a job through this
portal -- no code change needed.

**Job-detail page structure** (reverse-engineered from the 3 real closed
postings found above -- this is the only real data available to verify
against, since the live list is empty):
  - Title: the page's only `<h1>`.
  - Posting date: `<p class="text-xs ..."><span>Posted on </span><span>
    3 May 2026</span></p>` -- absolute date, `"%d %B %Y"` format.
  - A 4-row icon sidebar in a fixed order: department (e.g. "Nykaa
    Fashion"), location (bare city, lowercase, e.g. "bangalore" -- no
    "India" word), experience ("1 years Exp."), work mode ("In Office").
    Rows are identified defensively (by content pattern, not raw position)
    so a reordering wouldn't silently break location detection: the
    "N years Exp." row and the work-mode row (against a small vocabulary)
    are excluded first, then whichever remaining row matches a known India
    city/region token (Lenskart/Macquarie whitelist convention) is taken as
    location; the other remaining row is the department, which is not used.
  - Description: `<div class="job-description-panel">` contains the real
    content wrapped in one or more `<div class="page">...</div>` blocks,
    **followed by a verbatim duplicate of the same paragraphs as loose
    sibling `<p>` tags** (looks like a print/pagination artifact of
    whatever rich-text editor produced the posting). Only the `div.page`
    children are extracted to avoid double-counting every job's
    description text.

**Speculative, unverified pagination:** the list page currently has zero
jobs, so no pagination UI has ever been observed live. `_fill_cache` follows
an explicit "next page" link (`rel="next"` or link text "Next") if the site
ever renders one, capped at `_MAX_LIST_PAGES`; if no such link appears (the
observed case today, and likely for a while at this company's job volume),
a single page is treated as the whole pool -- same "small board, cache
once" idiom as Groww/Razorpay/CRED/Meesho/Paytm/Lenskart. Re-check this if
Nykaa's listing ever grows past what fits on one page.

**Location normalisation:** matcher.py's `is_india_job()` requires a literal
"india" substring; Nykaa's detail-page location text never includes it
(observed: "bangalore"). Following the Lenskart/Macquarie/Invesco
convention, ", India" is appended only when the location text matches a
whitelisted India city/region token -- Nykaa is a 100%-India-headquartered
company (Mumbai HQ) with no evidence of overseas engineering offices, so
this whitelist is deliberately generous (covers "remote"/"pan india" too),
but still only rewrites recognised tokens rather than blindly trusting every
string.

**Resilience to per-job failures:** each job in the cached list requires its
own detail-page fetch to get an authoritative location/date (the list page
itself, even when populated, is not guaranteed to expose location in a
stable, scrapable way from a zero-job sample -- only the detail page has
been directly verified). If one job's detail fetch fails, that job is kept
with a best-effort title recovered from the list page's own link text and a
blank location/date rather than aborting the whole company's fetch --
matcher.py's `is_india_job()` will simply (and safely) skip it.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE = "https://careers.nykaa.com"
_LIST_URL = f"{_BASE}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Job-detail URLs are flat UUIDs directly under the domain root, e.g.
# https://careers.nykaa.com/54a30b39-e12b-4df4-9496-e4e56729eb8d
_JOB_HREF_RE = re.compile(
    r"^/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$"
)

# "Posted on " <span> ... <span> 3 May 2026 </span> -- verified against 3 real
# job pages live 2026-08-31.
_POSTED_ON_RE = re.compile(
    r"Posted on\s*</span>\s*<span>\s*([^<]+?)\s*</span>", re.IGNORECASE
)

_EXPERIENCE_RE = re.compile(r"\bexp\b", re.IGNORECASE)
_WORK_MODE_TOKENS = {"in office", "remote", "hybrid", "work from home", "on-site", "onsite"}

# India city/region tokens observed (or plausible for a Mumbai-HQ, India-only
# company) on this ATS's bare, country-less location strings.
_INDIA_LOCATION_TOKENS = (
    "bangalore", "bengaluru", "mumbai", "navi mumbai", "thane", "gurugram",
    "gurgaon", "delhi", "ncr", "noida", "hyderabad", "pune", "chennai",
    "kolkata", "jaipur", "chandigarh", "kochi", "trivandrum", "ahmedabad",
    "pan india", "remote", "india",
)

_MAX_LIST_PAGES = 20  # defensive cap; speculative -- see module docstring
_DETAIL_FETCH_DELAY = 0.15


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


# Module-level cache: the full board is fetched once (list + every job's
# detail page) and reused for every keyword/page call, same "cache-once"
# idiom as lenskart_fetcher.py / razorpay_fetcher.py / cred_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}  # id -> (description, posting_date)
_cache_filled = False


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Nykaa: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Nykaa request failed: {exc}") from exc
    raise RateLimitError(f"Nykaa request failed: {last_exc}")


def _parse_posted_on(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _is_india_location(loc: str) -> bool:
    low = loc.lower()
    return any(tok in low for tok in _INDIA_LOCATION_TOKENS)


def _normalize_location(raw_loc: str) -> str:
    loc = (raw_loc or "").strip()
    if loc and "india" not in loc.lower() and _is_india_location(loc):
        return f"{loc}, India"
    return loc


def _extract_job_stubs(html: str) -> list[dict]:
    """Return [{"id", "href", "list_title"}] for every job link on a list page."""
    soup = BeautifulSoup(html, "html.parser")
    stubs: list[dict] = []
    seen_ids: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _JOB_HREF_RE.match(a["href"])
        if not m:
            continue
        job_id = m.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        stubs.append({
            "id": job_id,
            "href": f"/{job_id}",
            "list_title": a.get_text(strip=True),
        })
    return stubs


def _find_next_page_url(html: str, current_url: str) -> str | None:
    """Speculative pagination detection -- see module docstring.

    Follows an explicit rel="next" link or a link whose visible text is
    "Next"/"Next page". Never guessed against real populated data (the live
    board is currently empty), so this is a best-effort safety net rather
    than a verified contract.
    """
    soup = BeautifulSoup(html, "html.parser")
    next_link = soup.find("a", attrs={"rel": "next"})
    if next_link is None:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if text in ("next", "next page", ">"):
                next_link = a
                break
    if next_link is None:
        return None
    href = next_link.get("href") or ""
    if not href or href == current_url:
        return None
    if href.startswith("http"):
        return href
    return f"{_BASE}{href}" if href.startswith("/") else f"{_BASE}/{href}"


def _extract_description(soup: BeautifulSoup) -> str:
    """Pull job-description text, skipping the verbatim duplicate paragraphs.

    job-description-panel wraps the real content in one or more
    `div.page` blocks, then repeats the same paragraphs again as loose
    sibling <p> tags (a print/pagination artifact) -- only the div.page
    children are used so descriptions aren't doubled.
    """
    panel = soup.select_one("div.job-description-panel")
    if panel is None:
        return ""
    pages = panel.select("div.page")
    if not pages:
        return " ".join(panel.get_text(separator=" ").split())

    parts: list[str] = []
    for page in pages:
        paras = [p.get_text(strip=True) for p in page.find_all("p")]
        paras = [p for p in paras if p]
        if paras:
            parts.append("\n".join(paras))
    return "\n\n".join(parts)


def _extract_location(soup: BeautifulSoup) -> str:
    """Classify the 4-row icon sidebar defensively (see module docstring)."""
    candidates: list[str] = []
    for row in soup.find_all("div", class_="flex"):
        classes = row.get("class") or []
        if "items-center" not in classes or "space-x-2" not in classes:
            continue
        text = " ".join(row.get_text(separator=" ").split())
        if not text:
            continue
        candidates.append(text)

    remaining: list[str] = []
    for text in candidates:
        low = text.lower()
        if _EXPERIENCE_RE.search(low):
            continue
        if low in _WORK_MODE_TOKENS:
            continue
        remaining.append(text)

    for text in remaining:
        if _is_india_location(text):
            return text.strip()
    # Fallback: second remaining row is location in every observed sample
    # (department first, location second).
    if len(remaining) >= 2:
        return remaining[1].strip()
    return ""


def _fetch_job_detail(job_id: str, href: str, list_title: str, timeout: int) -> dict:
    url = f"{_BASE}{href}"
    try:
        r = _get(url, timeout)
    except RateLimitError as exc:
        print(f"[Nykaa] detail fetch failed for {job_id}: {exc}; using list fallback")
        _desc_cache[job_id] = ("", "")
        return {
            "id": job_id,
            "title": list_title,
            "location": "",
            "posting_date": "",
            "application_url": url,
        }

    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else list_title

    date_match = _POSTED_ON_RE.search(r.text)
    posting_date = _parse_posted_on(date_match.group(1)) if date_match else ""

    location = _normalize_location(_extract_location(soup))
    description = _extract_description(soup)

    _desc_cache[job_id] = (description, posting_date)

    return {
        "id": job_id,
        "title": title,
        "location": location,
        "posting_date": posting_date,
        "application_url": url,
    }


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Nykaa (Skima AI) board once and cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent lesson -- see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    all_stubs: list[dict] = []
    seen_ids: set[str] = set()
    url = _LIST_URL
    for _ in range(_MAX_LIST_PAGES):
        r = _get(url, timeout)  # list-page failure is fatal -- propagate
        stubs = _extract_job_stubs(r.text)
        new_count = 0
        for stub in stubs:
            if stub["id"] in seen_ids:
                continue
            seen_ids.add(stub["id"])
            all_stubs.append(stub)
            new_count += 1

        next_url = _find_next_page_url(r.text, url)
        if not next_url or new_count == 0:
            break
        url = next_url
        time.sleep(0.2)

    collected: list[dict] = []
    for stub in all_stubs:
        job = _fetch_job_detail(stub["id"], stub["href"], stub["list_title"], timeout)
        if job["title"]:
            collected.append(job)
        time.sleep(_DETAIL_FETCH_DELAY)

    _job_cache[:] = collected
    print(f"[Nykaa] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Nykaa jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    the whole board is small enough (and currently empty -- see module
    docstring) to cache in full and let matcher.py's own filters do the
    work, same as Lenskart/Razorpay/CRED/Meesho/Paytm.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    path = (application_url or "").split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Nykaa job.

    Served from the cache filled by fetch_jobs()/_fill_cache() when
    available; falls back to a direct fetch of application_url for a job
    that isn't (or is no longer) in the cached pool -- Nykaa's job-detail
    pages remain fully reachable even after a posting closes (verified
    against 3 real closed postings), so this never 404s on a stale seen-job
    URL the way some other ATSes do.
    """
    _fill_cache(timeout=timeout)

    job_id = _job_id_from_url(application_url)
    if job_id in _desc_cache:
        return _desc_cache[job_id]

    try:
        r = _get(application_url, timeout)
    except RateLimitError:
        return "", ""

    soup = BeautifulSoup(r.text, "html.parser")
    description = _extract_description(soup)
    date_match = _POSTED_ON_RE.search(r.text)
    posting_date = _parse_posted_on(date_match.group(1)) if date_match else ""
    _desc_cache[job_id] = (description, posting_date)
    return description, posting_date
