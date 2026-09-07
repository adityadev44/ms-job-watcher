"""Fetches Reliance Industries Limited (RIL) job listings from its own
legacy ASP.NET WebForms careers portal (careers.ril.com/rilcareers).

NOTE: this is Reliance Industries' OWN corporate/conglomerate careers portal
(oil & gas, petrochemicals, manufacturing, retail-parent, Reliance
Foundation, etc.) -- NOT Jio Platforms, which already has its own separate
fetcher (``jioplatforms_fetcher.py``) for Jio's own tech-heavy careers site.

ATS discovery (live, 2026-09-07): ril.com/careers links to
careers.ril.com/rilcareers/index.aspx, a homegrown ASP.NET WebForms site
(ViewState/__doPostBack, no recognizable ATS vendor). The homepage's
"Search Opportunities" form posts back with the keyword/location/function
fields, and the server responds by redirecting to
``frmJobSearch.aspx?func=...&loc=...&expreq=...&flag=...`` where those four
query-string values are RIL's own opaque encrypted blobs (observed to change
per search -- confirmed live: searching "software" vs "" vs "engineer"
produces three different ``expreq``/``func`` values). Replicating that
encrypted redirect with plain ``requests`` was NOT successful (a manually
constructed postback to index.aspx with a real ASP.NET ``__EVENTTARGET`` for
the search button rendered the unchanged homepage, not a redirect); it *did*
work end-to-end when driven live through Playwright (confirmed: keyword
"engineer" narrowed the visible 16-job board down to exactly the one posting
whose title literally contains "engineer" -- "Engineer Planning Mfg", not the
"Const Engr" postings -- so keyword search is a genuine, if literal
substring, server-side filter). This confirms the ATS *can* filter by
keyword, but only through machinery this fetcher does not reverse-engineer.

The crucial discovery that avoids needing Playwright for this fetcher at
all: ``frmJobSearch.aspx`` -- the exact same results page the encrypted
redirect lands on -- also serves the complete, unfiltered board on a bare
``GET`` with NO query string at all, live-confirmed reproducible and
identical across multiple fresh sessions with no cookies required. So this
fetcher hits ``frmJobSearch.aspx`` directly (skipping index.aspx and the
encrypted redirect entirely) and treats the un-filtered board as the full
candidate pool -- same "cache once per process, let matcher.py's own
title/skill filters narrow it" idiom already used by
``airindia_fetcher.py``/``birlasoft_fetcher.py``, and justified doubly here
since the whole live board is only 16 postings (2 pages of the default
ASP.NET GridView pager, 10/page).

Pagination: the results GridView's "Next" button
(``id="MainContent_rgJobs_lnkNext"``) is a plain ASP.NET ``<input
type="submit">`` postback control (no AJAX/UpdatePanel involved) -- a normal
``requests.Session`` POST replaying the page's own hidden ``__VIEWSTATE``/
``__EVENTVALIDATION`` plus that button's ``name`` (its numbered control ID,
e.g. ``ctl13`` on page 1 vs ``ctl09`` on page 2 -- NOT stable across pages,
so it must be read fresh off each page rather than hard-coded) reliably
advances to the next page -- confirmed live for both pages of the current
16-job board. The button carries ``disabled="disabled"`` on the final page,
which is this fetcher's real stop condition (a `_FIRST_PAGE_IDS` replay
guard is also kept as a defensive backstop).

HTML quirk that breaks BeautifulSoup: every job-title ``<a>`` tag is
rendered with the SAME attribute (``href``) written out TWICE -- once with
the real (encrypted-query-string) detail-page link, once with a duplicate
``href`` whose value is just the URL-encoded title text. Python's stdlib
``html.parser`` (bs4's only available backend in this environment) resolves
duplicate attributes by keeping the LAST one seen, silently returning the
useless title-echo string from ``.get("href")`` instead of the real link.
Row extraction here therefore uses a raw-text regex anchored on the
``hylUser_<n>`` id (which captures the FIRST, correct href) rather than
BeautifulSoup's parsed attribute dict.

Job "id": the encrypted ``JBTITLE``/``jbID`` query params are opaque, but
every job title already ends in a bracketed real requisition number, e.g.
"SPM Officer ( 82966035 )" -- that plain, stable, human-legible number is
used as ``id`` instead of hashing/reusing the encrypted blobs.

Location: bare city names with no "India" substring at all (e.g.
"Jamnagar", "Navi Mumbai", "Surat", "Vadodara") -- every posting observed
live across both pages of the current board is one of a small set of Indian
manufacturing/corporate hub cities; those are normalized to append ", India"
and anything unrecognized is left unchanged (fails `is_india_job()` safely
rather than guessing, same defensive posture as `hdfcbank_fetcher.py`).

Signal-to-noise: the live board (16 total postings, all India, all on this
one un-filtered GET) is entirely refinery/manufacturing/O&G operations,
corporate-affairs and R&D roles (SPM Officer, Field Executive, Const Engr,
Research Scientist, HR Associate, Secretary, Duty Port Captain, etc.) --
zero postings in a software/IT title family right now. This is a genuine
"zero is a fact" result for this portal today (same as Darwinbox's own
board elsewhere in this repo) -- RIL's tech/digital hiring for Jio
Platforms/Reliance Retail's tech arm goes through their own separate
career sites, not this corporate WebForms portal. The pipeline is
mechanically correct and will surface a real match automatically the
moment RIL posts one here with a matching title.

Detail page (``frmJobSearch.aspx?JBTITLE=...&jbID=...``, confirmed
reachable directly via plain GET with no session/cookies needed): full JD
text lives across four separate ``<span>`` blocks --
``#MainContent_lblSummRole`` (responsibilities), ``#MainContent_lblEduReq``,
``#MainContent_lblExpReq``, ``#MainContent_lblSkill`` -- concatenated here
into one description. Posting date is also re-read from
``#MainContent_lblPostedDate`` ("03 Sep 2026" -> "2026-09-03").
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.ril.com/rilcareers"
_SEARCH_URL = f"{_BASE_URL}/frmJobSearch.aspx"

_PAGE_SIZE = 10  # GridView default page size on this tenant; not configurable
_MAX_PAGES = 30  # safety ceiling comfortably above the current ~16-job pool

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_ROW_RE = re.compile(
    r'<a id="[^"]*hylUser_\d+" href="([^"]+)"[^>]*>([^<]+)</a>\s*'
    r'</td><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td>',
    re.DOTALL,
)
_REQ_ID_RE = re.compile(r"\(\s*(\d+)\s*\)\s*$")
_RESULTS_CONTAINER_MARKER = 'id="MainContent_rgJobs"'
_NEXT_BTN_RE = re.compile(
    r'<input type="submit" name="([^"]*lnkNext)"[^>]*id="MainContent_rgJobs_lnkNext"([^>]*)/?>'
)

_INDIA_CITIES = {
    "jamnagar", "navi mumbai", "mumbai", "surat", "vadodara", "bangalore",
    "bengaluru", "gurgaon", "gurugram", "delhi", "new delhi", "hazira",
    "hyderabad", "chennai", "pune", "kolkata", "ahmedabad", "nagpur",
    "nagothane", "patalganga", "dahej", "jamnagar refinery",
}

# Module-level cache: filled once, reused for every keyword call in this
# process (keywords are ignored -- see module docstring).
_job_cache: list[dict] = []
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None

# Pagination-wraparound guard (see repo contract).
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent connection/parse failure from RIL's portal."""


def _parse_date(raw: str) -> str:
    """Convert '03 Sep 2026' -> '2026-09-03'."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalise_location(raw: str) -> str:
    """Add India only for a recognized city; preserve unknown/overseas values."""
    loc = " ".join((raw or "").split())
    if not loc:
        return loc
    if re.search(r"\bindia\b", loc, re.IGNORECASE):
        return loc
    if loc.strip().casefold() in _INDIA_CITIES:
        return f"{loc}, India"
    return loc


def _hidden_fields(html_text: str) -> dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    fields: dict[str, str] = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype in ("checkbox", "radio") and not inp.get("checked"):
            continue
        if itype == "submit":
            continue
        fields[name] = inp.get("value", "")
    for sel in soup.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        fields[name] = opt.get("value", "") if opt else ""
    return fields


def _request(session: requests.Session, method: str, timeout: int, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = session.request(method, _SEARCH_URL, headers=_HEADERS, timeout=timeout, **kwargs)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Reliance: 429 rate-limited during cache fill")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Reliance cache fill failed: {exc}") from exc
    raise RateLimitError(f"Reliance cache fill: no response -- {last_exc}")


def _parse_rows(html_text: str) -> list[dict]:
    rows: list[dict] = []
    for href, title_raw, func_raw, loc_raw, date_raw in _ROW_RE.findall(html_text):
        href = html_mod.unescape(href)
        title = html_mod.unescape(title_raw).strip()
        if not title or not href:
            continue
        m = _REQ_ID_RE.search(title)
        job_id = m.group(1) if m else title
        location = _normalise_location(html_mod.unescape(loc_raw).strip())
        rows.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": _parse_date(html_mod.unescape(date_raw).strip()),
            "application_url": f"{_BASE_URL}/{href}",
        })
    return rows


def _next_button_name(html_text: str) -> str | None:
    m = _NEXT_BTN_RE.search(html_text)
    if not m:
        return None
    name, trailing = m.groups()
    if "disabled" in trailing:
        return None
    return name


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the whole un-filtered board once and cache every job.

    ``_cache_filled`` is set before the loop so a mid-fetch failure doesn't
    trigger a retry storm on every subsequent keyword call.
    """
    global _job_cache, _cache_filled, _FIRST_PAGE_IDS, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    session = requests.Session()
    collected: list[dict] = []
    seen_ids: set[str] = set()
    first_page_ids: set[str] | None = None

    try:
        r = _request(session, "GET", timeout)
    except RateLimitError as exc:
        _cache_error = exc
        raise

    for page_num in range(_MAX_PAGES):
        if page_num > 0:
            time.sleep(0.15)

        html_text = r.text
        if page_num == 0 and _RESULTS_CONTAINER_MARKER not in html_text:
            _cache_error = RateLimitError(
                "Reliance: results table missing from response -- blocked or malformed page"
            )
            raise _cache_error

        rows = _parse_rows(html_text)
        page_ids = {row["id"] for row in rows}

        if page_num == 0:
            first_page_ids = page_ids
            _FIRST_PAGE_IDS = first_page_ids
        elif first_page_ids and page_ids and page_ids == first_page_ids:
            break  # ATS silently replayed page 1

        new_this_page = 0
        for row in rows:
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            new_this_page += 1
            collected.append(row)

        if not rows or new_this_page == 0:
            break

        next_name = _next_button_name(html_text)
        if not next_name:
            break  # final page -- Next button missing or disabled

        fields = _hidden_fields(html_text)
        fields[next_name] = "Next"
        try:
            r = _request(session, "POST", timeout, data=fields)
        except RateLimitError as exc:
            _cache_error = exc
            raise

    _job_cache = collected
    print(f"[Reliance] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Reliance Industries jobs.

    Keywords are ignored (see module docstring) -- the whole board (~16
    postings) is cached once per process and matcher.py's shared
    title/skill filters do the real narrowing, even though the ATS's own
    keyword search genuinely (if literally, substring-only) filters
    server-side through machinery this fetcher does not replicate.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description and posting date from a detail page.

    Returns (description_text, posting_date) where posting_date is
    YYYY-MM-DD. Concatenates the four separate JD sections this tenant
    splits the description across (responsibilities/education/experience/
    skills), and re-reads the posting date from `#MainContent_lblPostedDate`.
    """
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Reliance description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Reliance description fetch failed: {exc}") from exc

    if r is None:
        return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    parts = []
    for span_id in (
        "MainContent_lblSummRole",
        "MainContent_lblEduReq",
        "MainContent_lblExpReq",
        "MainContent_lblSkill",
    ):
        span = soup.find(id=span_id)
        if span:
            raw = html_mod.unescape(span.get_text(" ", strip=True))
            text = " ".join(raw.split())
            text = re.sub(r"^\.\s*", "", text).strip()
            if text:
                parts.append(text)
    description = " ".join(parts)

    posting_date = ""
    date_span = soup.find(id="MainContent_lblPostedDate")
    if date_span:
        posting_date = _parse_date(date_span.get_text(strip=True))

    return description, posting_date
