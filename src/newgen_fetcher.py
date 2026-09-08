"""Fetches Newgen Software job listings from its own "OmniRecruit" ATS.

ATS discovery (2026-09-08): newgensoft.com/company/careers/ links out to
`https://omnirecruit.newgen.co.in/IShareReferral/CareerPortal.aspx` -- an
in-house ASP.NET Web Forms career portal (Newgen builds BPM/low-code
products, and OmniRecruit appears to be their own internally-branded
recruitment module, not a third-party ATS vendor). No REST/JSON API is
exposed to the page's own JS -- the entire job list, including the FULL
description text for every posting, is server-rendered directly into the
page HTML inside repeated `<div id="omnirecruit_referFadeEffects">` panels
(both the truncated "...More" and expanded "...Less" variants of each
panel actually already carry the complete description server-side --
there is no truncation to work around).

Pagination is a classic ASP.NET Web Forms `__doPostBack` DataPager
(`Total Records :22` on 2026-09-08, ~10 shown per page). Replaying the
pager's postback (with `__VIEWSTATE`/`__EVENTVALIDATION` carried over from
a prior GET, plus the `ScriptManager` async-postback fields) did not
actually advance to page 2/3 in testing -- it silently re-served the same
first page. However, the portal's own `txtSearchKeyWord` search field DOES
filter server-side (confirmed live: "developer" narrows the visible set
from 10 to 2, distinct queries surface different subsets), so the full
~22-job board was recovered by unioning the results of many overlapping
keyword probes by job ID. Since the whole board is small and stable, the
fetcher caches the full pool once per process (like `juspay_fetcher.py` /
Deutsche Bank) rather than repeating this per configured keyword --
`keyword`/`location` args are accepted for interface compatibility but
ignored (registered in `_IGNORES_KEYWORDS`).

Every posting's location text (from the `<i class="fa-map-marker">`
sibling) already includes ", India" for every India office observed
(Noida, Mumbai, Chennai) -- no city-token normalization needed, unlike
Invesco/Finastra. One data-quality quirk noted for the record: the
"Software Engineer/Senior Software Engineer" posting's location badge
says "Mumbai, India" but its own description body says "Location: PUNE"
-- a mislabeling on Newgen's side, not something this fetcher can correct
(the location badge is what's used for matching, consistent with what a
human applicant sees on the listing card).

Application URL: each panel's "APPLY" button calls
`fRedirect(encodeURIComponent('<jobId>'))`, which client-side-navigates to
`/CandidateRegistration/CandidateRegistration.aspx?JobExternalUsers=<jobId>`
-- used directly as `application_url`.

Live board content at time of writing (22 total India postings; Noida,
Mumbai, Chennai) skews sales/support/admin/BA with a handful of
engineering-adjacent roles (Oracle DBA/WebLogic admin, AWS Cloud Support,
Cyber Security, one Java-only "Software Engineer/Senior Software
Engineer") -- none carry .NET/C#/ASP.NET or AI/ML/Python signal today.
Zero live matches is a legitimate current-state result (same shape as
Sprinklr/Juspay in this repo), not a fetcher bug -- the wiring itself is
confirmed correct and will surface future .NET/AI postings automatically.
"""

from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

_BASE = "https://omnirecruit.newgen.co.in"
_PORTAL_URL = f"{_BASE}/IShareReferral/CareerPortal.aspx"
_APPLY_BASE = f"{_BASE}/CandidateRegistration/CandidateRegistration.aspx?JobExternalUsers="

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# Overlapping search terms used to union-probe the full board by job ID --
# the portal's keyword search genuinely filters server-side (narrower terms
# return fewer, different postings), so no single request reliably returns
# every posting; broad common substrings across title/keyskills/description
# converge on the full pool in practice (verified live: 22/22 recovered).
_PROBE_TERMS = (
    "", "software", "developer", "engineer", "manager", "analyst", "support",
    "sales", "designer", "trade", "cloud", "security", "java", "oracle",
    "insurance", "lending", "marketing", "consultant", "admin", "specialist",
    "a", "e",
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure against OmniRecruit."""


def _parse_posted_on(text: str) -> str:
    """Convert 'Posted On: 07 Aug 2026' to '2026-08-07'."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{4})", text or "")
    if not m:
        return ""
    day, mon, year = m.groups()
    month = _MONTHS.get(mon.lower()[:3])
    if not month:
        return ""
    return f"{year}-{month:02d}-{int(day):02d}"


def _get_form_state(session: requests.Session, timeout: int) -> tuple[BeautifulSoup, dict]:
    r = session.get(_PORTAL_URL, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    data: dict[str, str] = {}
    if form is None:
        return soup, data
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = inp.get("type", "text")
        if itype in ("checkbox", "radio") and not inp.get("checked"):
            continue
        data[name] = inp.get("value", "")
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        data[name] = opt.get("value", "") if opt else ""
    return soup, data


def _search(keyword: str, timeout: int) -> str:
    session = requests.Session()
    _, data = _get_form_state(session, timeout)
    if keyword:
        data["dataListDiv$txtSearchKeyWord"] = keyword
    data.pop("dataListDiv$btnReferClear", None)
    r = session.post(_PORTAL_URL, headers=_HEADERS, data=data, timeout=timeout)
    r.raise_for_status()
    return r.text


def _parse_panels(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    panels = soup.find_all("div", id="omnirecruit_referFadeEffects")
    out = []
    for p in panels:
        headcount = p.find(class_="headcount")
        title = headcount.get_text(strip=True) if headcount else ""
        if not title:
            continue

        loc_icon = p.find("i", class_="fa-map-marker")
        loc = ""
        if loc_icon and loc_icon.parent:
            loc = loc_icon.parent.get_text(strip=True).replace("\xa0", "").replace(" ", "")
        if not loc:
            continue

        apply_a = p.find("a", onclick=re.compile(r"fRedirect\("))
        job_id = ""
        if apply_a:
            m = re.search(r"fRedirect\(encodeURIComponent\('([^']+)'\)\)", apply_a.get("onclick", ""))
            if m:
                job_id = m.group(1)
        if not job_id:
            continue

        posted_node = p.find(string=re.compile("Posted On"))
        posted_text = posted_node.parent.get_text(strip=True) if posted_node else ""

        desc_divs = p.find_all("div", class_="panel-body")
        desc_text = ""
        for d in desc_divs:
            if "pre-line" in (d.get("style") or ""):
                desc_text = d.get_text(separator="\n", strip=True)
                break
        if not desc_text and desc_divs:
            desc_text = desc_divs[0].get_text(separator="\n", strip=True)
        desc_text = " ".join(desc_text.split())
        # strip the trailing "...Less"/"...More" toggle-link text
        desc_text = re.sub(r"\s*\.\.\.(Less|More)\s*$", "", desc_text)

        out.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": _parse_posted_on(posted_text),
            "application_url": f"{_APPLY_BASE}{job_id}",
            "_description": desc_text,
        })
    return out


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: dict[str, dict] = {}
    last_exc: Exception | None = None
    got_any = False

    for term in _PROBE_TERMS:
        for attempt in range(2):
            try:
                html = _search(term, timeout)
                got_any = True
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1)
                    continue
        else:
            continue

        for job in _parse_panels(html):
            job_id = job["id"]
            if job_id not in collected:
                _desc_cache[job_id] = job.pop("_description")
                collected[job_id] = job
            else:
                job.pop("_description", None)

    if not got_any:
        raise RateLimitError(f"Newgen cache fill: all probes failed — {last_exc}")

    _job_cache[:] = list(collected.values())
    print(f"[Newgen] Cache filled: {len(_job_cache)} total India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Newgen jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored
    after the initial cache fill -- see module docstring for why the board
    is cached in full rather than queried per keyword.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) served from the fetch_jobs() cache."""
    _fill_cache(timeout=timeout)
    m = re.search(r"JobExternalUsers=([^&]+)", application_url)
    job_id = m.group(1) if m else ""
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
