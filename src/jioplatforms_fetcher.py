"""Fetches Jio Platforms job listings via careers.jio.com.

This is a legacy ASP.NET WebForms site (same generation as Tech Mahindra's
careers.techmahindra.com) but turns out to need only plain `requests` — no
Playwright/Firefox required.

Investigation findings:
- `frmJobCategories.aspx` (the entry page) server-renders 24 job-function
  "category" tiles (Engineering & Technology, IT & Systems, Sales and
  Distribution, Freelancer, ...), each with a job count and a link to
  `frmfuncwisejob.aspx?func=<enc>&desc=<enc>&flag=<enc>` carrying encrypted-
  but-STATIC per-category tokens (verified working from a brand-new session
  with zero prior requests — they are not session-scoped, unlike a
  `__VIEWSTATE`).
- The page's own "Refine your search" keyword/location/function/freshness
  form is a red herring for real search: submitting it always POSTs back to
  `frmJobCategories.aspx` and only narrows which CATEGORY TILES are shown, by
  a plain substring match against the category *display name* (e.g. keyword
  "engineer" surfaces only the "Engineering & Technology" tile because
  "engineer" ⊂ "Engineering", not because any job title/description was
  matched). It never filters actual job listings, and an empty keyword or a
  free-text "India" location returns "No job found" outright. So keyword AND
  location are both ignored server-side for real filtering purposes — see
  `fetch_jobs`.
- `frmfuncwisejob.aspx` (the actual per-category job list) IS a real job
  listing with plain server-rendered rows (title+id, city, function, posted
  date, and a link to `frmjobdescription.aspx`) — no JS rendering needed.
- Pagination (`ddlentries` page-size dropdown, `DataPager1` Next/Prev) fires
  through `__doPostBack`/plain `<input type=submit>` controls, but — unlike
  Tech Mahindra's country dropdown — `Sys.WebForms.PageRequestManager
  ._initialize(...)` on this page is called with EMPTY postback/async-
  postback control arrays, meaning there is no UpdatePanel wrapping anything:
  every postback here is a full, ordinary page postback. Replicated with
  plain `requests` by resubmitting the ENTIRE previous page's form field set
  (every hidden field, not just __VIEWSTATE/__EVENTVALIDATION — dropping any
  of them makes the server 302 to a generic "Your session has timed out!"
  page instead of a useful error) plus the one changed field
  (`ddlentries` or the pager's `lnkNext` button name/value).
- `frmjobdescription.aspx` (job detail) is plain server-rendered HTML and
  needs no session/cookie context at all — confirmed fetching it from a
  cold `requests.Session()` with zero prior cookies.

Scope decision: of the 24 categories, only "Engineering & Technology" (~249
jobs) and "IT & Systems" (~13 jobs) can plausibly contain software-engineer
titles that would ever pass matcher.py's `title_family` check. The other 22
categories (Freelancer ~7800, Freelancer - Sales Associate ~7700, Jio Sales
Associate ~6700, Business Operations ~6000, Sales and Distribution ~800,
Customer Service ~620, ...) total in the tens of thousands and hold zero
titles that could ever match "software engineer"/"SDE"/"backend engineer"/
etc. — paginating through them would be pure waste (Persistent/Honeywell
lesson: don't do slow work with no possible payoff). If Jio later opens a
dedicated AI/ML/data-science category, this scope will need revisiting.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE = "https://careers.jio.com"
_FLAG = "/wASbQn4xyQ="
_PAGE_SIZE = "25"
_MAX_PAGES = 40  # safety ceiling (25/page -> 1000 jobs; category totals are ~250)

# (display name, encrypted func token, encrypted desc token) — hardcoded from
# a live GET of frmJobCategories.aspx's category tiles. Confirmed static
# (not session-bound) but could theoretically rotate if Jio redeploys the
# encryption key; if fetch_jobs() ever returns 0 for both categories, refetch
# these from the raw category page's <a href="frmfuncwisejob.aspx?..."> links.
_CATEGORIES = [
    ("Engineering & Technology", "09Bqkj0vwzk=", "tBOU2f2ubJIKJJaEorlljoC0jBhJb9cLpWXiiP5HyBU="),
    ("IT & Systems", "2pGaMqAexDY=", "am1AraTXtX4/W/EJK6dzzw=="),
]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}

_TITLE_RE = re.compile(r'hylUser_\d+"\s+href="([^"]+)">([^<]+)</a>')
_LOC_RE = re.compile(r'Label2_\d+">([^<]*)</span>')
_DATE_RE = re.compile(r'Label1_\d+">([^<]*)</span>')
_TITLE_ID_RE = re.compile(r"^(.*)\(\s*(\d+)\s*\)\s*$")


class RateLimitError(Exception):
    """Raised on 429s or persistent network/postback failure."""


# ---------------------------------------------------------------------------
# HTTP helpers — 3-attempt retry with exponential backoff
# ---------------------------------------------------------------------------

def _get(session: requests.Session, url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = session.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Jio Platforms: 429 rate-limited GET {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Jio Platforms GET failed ({url}): {exc}") from exc
    raise RateLimitError(f"Jio Platforms GET: no response — {last_exc}")


def _post(session: requests.Session, url: str, data: dict, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    headers = {
        **_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": url,
        "Origin": _BASE,
    }
    for attempt in range(3):
        try:
            r = session.post(url, data=data, headers=headers, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Jio Platforms: 429 rate-limited POST {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Jio Platforms POST failed ({url}): {exc}") from exc
    raise RateLimitError(f"Jio Platforms POST: no response — {last_exc}")


def _form_fields(html: str) -> dict[str, str]:
    """Collect every non-button form field so a postback replicates ASP.NET's
    full ViewState/EventValidation state exactly. Dropping any field here
    makes the server reject the postback with a generic "Your session has
    timed out!" page instead of a useful error (confirmed live)."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    data: dict[str, str] = {}
    if form is None:
        return data
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        if (inp.get("type") or "text").lower() in ("submit", "button", "image", "file"):
            continue
        data[name] = inp.get("value", "")
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        data[name] = opt.get("value", "") if opt else ""
    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name:
            data[name] = ta.text or ""
    return data


def _parse_date(raw: str) -> str:
    """Convert 'DD Mon YYYY' (e.g. '29 Aug 2026') -> 'YYYY-MM-DD'."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_jobs(html: str) -> list[dict]:
    hrefs_titles = _TITLE_RE.findall(html)
    locs = _LOC_RE.findall(html)
    dates = _DATE_RE.findall(html)

    jobs: list[dict] = []
    for i, (href_raw, title_raw) in enumerate(hrefs_titles):
        href = html_mod.unescape(href_raw)
        title_raw = title_raw.strip()
        m = _TITLE_ID_RE.match(title_raw)
        if m:
            title, job_id = m.group(1).strip(), m.group(2)
        else:
            title, job_id = title_raw, href  # fallback: use the URL itself as id

        loc = locs[i].strip() if i < len(locs) else ""
        location = f"{loc}, India" if loc else "India"
        posting_date = _parse_date(dates[i]) if i < len(dates) else ""

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": posting_date,
            "application_url": f"{_BASE}/{href}",
        })
    return jobs


def _scrape_category(session: requests.Session, func_token: str, desc_token: str, timeout: int) -> list[dict]:
    url = f"{_BASE}/frmfuncwisejob.aspx?func={func_token}&desc={desc_token}&flag={_FLAG}"
    html = _get(session, url, timeout).text

    # Bump page size to 25 up front to minimize request count.
    data = _form_fields(html)
    data["ctl00$MainContent$ddlentries"] = _PAGE_SIZE
    data["__EVENTTARGET"] = "ctl00$MainContent$ddlentries"
    data["__EVENTARGUMENT"] = ""
    html = _post(session, url, data, timeout).text

    collected: list[dict] = []
    seen_first_ids: set[str] = set()
    for _ in range(_MAX_PAGES):
        page_jobs = _parse_jobs(html)
        if not page_jobs:
            break
        if page_jobs[0]["id"] in seen_first_ids:
            break  # wraparound guard (Workday/BrassRing-style bug seen elsewhere in this repo)
        seen_first_ids.add(page_jobs[0]["id"])
        collected.extend(page_jobs)

        data = _form_fields(html)
        data["__EVENTTARGET"] = ""
        data["__EVENTARGUMENT"] = ""
        data["ctl00$MainContent$lstJoblist$DataPager1$ctl00$lnkNext"] = "Next"
        html = _post(session, url, data, timeout).text
        time.sleep(0.15)

    return collected


# ---------------------------------------------------------------------------
# Job-list cache — scrape both categories once, serve slices on every call
# ---------------------------------------------------------------------------

_jobs_cache: list[dict] = []
_cache_filled: bool = False


def _fill_cache(timeout: int = 20) -> None:
    global _jobs_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before fetching -- avoid a retry storm on failure (Honeywell lesson)

    session = requests.Session()
    session.headers.update(_HEADERS)

    collected: list[dict] = []
    seen_ids: set[str] = set()
    for _, func_token, desc_token in _CATEGORIES:
        for job in _scrape_category(session, func_token, desc_token, timeout):
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            collected.append(job)

    _jobs_cache = collected
    print(f"[Jio Platforms] Cache filled: {len(collected)} jobs across {len(_CATEGORIES)} categories")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a cached slice of Jio Platforms India jobs.

    keyword/location are both ignored: the site's own search form only
    filters which category tiles are shown (a substring match against
    category *names*, not job content — see module docstring) and even
    breaks outright ("No job found") on an empty keyword or free-text
    location, so it offers no real server-side filtering to delegate to.
    The two software-relevant categories are scraped in full once and cached
    in-module; matcher.py's title/skill checks do the actual filtering.
    """
    _fill_cache(timeout=timeout)
    return _jobs_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch a single job's full JD text and posting date.

    frmjobdescription.aspx is plain server-rendered HTML — confirmed working
    from a brand-new session with no prior cookies, no ViewState/postback
    dance needed here (only the category listing's pagination needs that).
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
                raise RateLimitError("Jio Platforms description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Jio Platforms description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Jio Platforms description fetch: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")

    def _label_text(elem_id: str) -> str:
        el = soup.find(id=elem_id)
        return el.get_text(" ", strip=True) if el else ""

    parts = []
    for elem_id, label in (
        ("MainContent_lblSummRole", "Job Responsibilities"),
        ("MainContent_lblEduReq", "Education Requirement"),
        ("MainContent_lblExpReq", "Experience Requirement"),
        ("MainContent_lblSkill", "Skills & Competencies"),
    ):
        text = _label_text(elem_id)
        if text:
            parts.append(f"{label}: {text}")
    description = " | ".join(parts)

    posting_date = _parse_date(_label_text("MainContent_lblPostedDate"))
    return description, posting_date
