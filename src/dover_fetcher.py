"""Fetches Dover Corporation job listings via the SAP SuccessFactors J2W
(classic) HTML search API.

Dover's India hiring lives at careers.dovercorporation.com. The top-level
www.dovercareers.com brand domain is fully Cloudflare-challenge-gated
(confirmed live: even headless Chromium with
``--disable-blink-features=AutomationControlled`` never got past the
"Just a moment..." managed challenge) -- but the real ATS is a completely
separate host, careers.dovercorporation.com, which turned out to be the same
classic J2W theme already known from Nomura/Capgemini/SAP Labs/Mastek: real
server-rendered ``<tr class="data-row">`` HTML on first load, no CSRF/session
dance needed, plain ``requests`` works. (This is another instance of the
playbook's "a company's obvious branded domain is not proof of where the ATS
actually lives" lesson -- the working host was found only via a job-board
aggregator link, not by guessing subdomains of dovercareers.com.)

Search endpoint: ``GET /search/?q=<kw>&locationsearch=India&startrow=<n>``

- ``locationsearch=India`` reliably restricts results to genuinely India-based
  postings -- verified live: every job returned this way is "Bengaluru, IN" /
  "Bengaluru, KA, IN", with none of the non-India leakage (Cebu City PH,
  Austin TX, Turnhout BE, Dundee UK, Barcelona ES) that shows up if you
  instead full-text-search ``q=India`` with no location filter at all.
- ``q=`` is NOT a reliable narrow keyword filter on this tenant -- it
  full-text-matches the entire boilerplate company blurb as well as the JD
  body, so common terms ("software engineer", "AI engineer", "generative ai
  engineer") return nearly the whole ~39-job India pool while others ("dot
  net", "angular") return only 2-3. Verified live that this is not safe to
  trust: the exact "Senior Software Engineer" C#/.NET Core/.NET Framework
  role this company was added for (job id 1419554833) is one of the ones
  narrowed away by some of the very keywords meant to find it (only 31/39
  under ".NET developer", 35/39 under "C# developer"). Ignored entirely --
  the whole small India pool is cached once per process and matcher.py's own
  title/skill checks do the real filtering, same reasoning already used for
  CRISIL's Zwayam tenant.
- Page size is a fixed 25/page; ``startrow=N`` pagination terminates cleanly
  with zero ``<tr class="data-row">`` rows once N exceeds the true total --
  no wraparound-past-total quirk here (unlike the UBS/MUFG/Nvidia/Pfizer/
  Walmart family of Workday tenants).

Detail pages: ``<span class="jobdescription">`` holds the full JD (appears
exactly once per page -- no Wipro/HCLTech-style duplicate-itemprop
boilerplate block to dodge) and
``<meta itemprop="datePosted" content="Wed Sep 02 02:00:00 UTC 2026">``.

Location format: "Bengaluru, IN" / "Bengaluru, KA, IN" -> normalised to end
in ", India" (the same "City, IN" -> "City, India" pattern already used for
Nomura/Capgemini's J2W tenants) so matcher.py's ``is_india_job()`` recognises
it -- the raw ISO country code alone never contains the substring "india".
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.dovercorporation.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # J2W always returns 25 per page; not configurable
_MAX_PAGES = 40  # safety ceiling well above the current ~39-job India pool

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Module-level cache: filled once, reused for every keyword call in this
# process (keywords are ignored -- see module docstring).
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from Dover's J2W site."""


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_search_date(raw: str) -> str:
    """Convert 'Sep 3, 2026' (search result) to '2026-09-03'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Wed Sep 02 02:00:00 UTC 2026' (meta tag) to '2026-09-02'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalise_location(raw: str) -> str:
    """'Bengaluru, KA, IN' -> 'Bengaluru, KA, India' (trailing ISO code only)."""
    if not raw:
        return "India"
    normalised = re.sub(r",\s*IN\s*$", ", India", raw.strip())
    if "india" not in normalised.lower():
        normalised = f"{normalised}, India"
    return normalised


# ---------------------------------------------------------------------------
# Cache fill
# ---------------------------------------------------------------------------

def _fetch_page(start: int, timeout: int) -> str:
    params = {"q": "", "locationsearch": "India"}
    if start:
        params["startrow"] = start

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Dover: 429 rate-limited during cache fill")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Dover cache fill failed: {exc}") from exc
    raise RateLimitError(f"Dover cache fill: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through the whole India search once and cache every job.

    ``_cache_filled`` is set before the loop so a mid-fetch failure doesn't
    trigger a retry storm on every subsequent keyword call (the Honeywell/
    Persistent lesson).
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    seen_ids: set[str] = set()
    for page_num in range(_MAX_PAGES):
        start = page_num * _PAGE_SIZE
        if page_num > 0:
            time.sleep(0.2)

        html_text = _fetch_page(start, timeout)
        soup = BeautifulSoup(html_text, "html.parser")
        rows = soup.select("tr.data-row")
        if not rows:
            break

        new_this_page = 0
        for row in rows:
            link = row.select_one("span.jobTitle.hidden-phone a.jobTitle-link")
            if not link:
                link = row.select_one("a.jobTitle-link")
            if not link:
                continue

            href = link.get("href", "").strip()
            title = html_mod.unescape(link.get_text(strip=True))
            if not href or not title:
                continue

            # Job ID: trailing numeric segment of the path, e.g.
            # "/job/Bengaluru-Senior-Software-Engineer-KA/1419554833/" -> "1419554833"
            job_id = href.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit() or job_id in seen_ids:
                continue

            loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
            if not loc_cell:
                loc_cell = row.select_one("span.jobLocation")
            loc_text = ""
            if loc_cell:
                for part in loc_cell.children:
                    raw_part = getattr(part, "string", None) or (
                        str(part) if hasattr(part, "strip") else ""
                    )
                    candidate = raw_part.strip()
                    if candidate and not candidate.startswith("+"):
                        loc_text = html_mod.unescape(candidate)
                        break
            loc_text = _normalise_location(loc_text)

            date_span = row.select_one("td.colDate.hidden-phone span.jobDate")
            if not date_span:
                date_span = row.select_one("span.jobDate")
            posting_date = _parse_search_date(date_span.get_text(strip=True)) if date_span else ""

            seen_ids.add(job_id)
            new_this_page += 1
            collected.append({
                "id": job_id,
                "title": title,
                "location": loc_text,
                "posting_date": posting_date,
                "application_url": f"{_BASE_URL}{href}",
            })

        if new_this_page == 0:
            # Every row on this page was already seen -- defensive guard
            # against a wraparound-past-total quirk, even though live
            # testing showed this tenant terminates cleanly instead.
            break

    _india_cache = collected
    print(f"[Dover] Cache filled: {len(collected)} India jobs")


# ---------------------------------------------------------------------------
# Public API expected by matcher.py
# ---------------------------------------------------------------------------

def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Dover India jobs.

    Keywords are ignored (see module docstring) -- the small India pool is
    cached once and matcher.py's shared title/skill filters do the real
    work.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Fetch the full job description and posting date from a detail page.

    Returns (description_text, posting_date) where posting_date is
    YYYY-MM-DD. The detail page uses <span class="jobdescription"> and
    <meta itemprop="datePosted" content="Wed Sep 02 02:00:00 UTC 2026">.
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Dover description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    if r is None:
        return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    desc_span = soup.select_one("span.jobdescription")
    description = ""
    if desc_span:
        raw = html_mod.unescape(desc_span.get_text(" ", strip=True))
        description = " ".join(raw.split())

    posting_date = ""
    date_meta = soup.find("meta", {"itemprop": "datePosted"})
    if date_meta:
        posting_date = _parse_detail_date(date_meta.get("content", ""))

    return description, posting_date
