"""Fetches Birlasoft job listings via the SAP SuccessFactors J2W (classic)
HTML search API.

Birlasoft's own marketing site (``www.birlasoft.com/careers``) is a Drupal
CMS page that links out to the real careers portal, ``jobs.birlasoft.com`` --
found only via a plain ``<a href="https://jobs.birlasoft.com/">`` link
buried in the marketing page's HTML, not by guessing subdomains (same
"the branded domain is not proof of anything" lesson as Dover/Boeing).
Live inspection (2026-09-06) of that portal confirms it is the exact same
classic J2W ("tile theme") self-hosted career site already known from
Dover/Nomura/Capgemini/SAP Labs/Mastek: real server-rendered
``<tr class="data-row">`` HTML on first load, no CSRF/session dance,
plain ``requests`` works. The apply/registration forms on the same page
post to ``career44.sapsf.com/career?...&company=birlasoftl``, and a JS
blob on the page sets ``ssoCompanyId: 'birlasoftl'`` -- both confirm the
SuccessFactors company ID and that this is J2W-fronted SuccessFactors, not
a bespoke ATS.

Search endpoint: ``GET /search/?q=<kw>&locationsearch=India&startrow=<n>``

- ``locationsearch=India`` is a clean, reliable server-side filter --
  verified live across the full ~591-job India pool: every single result's
  location string begins with "INDIA" (or, for one posting, "Birlasoft
  Limited, INDIA - ..."), with zero non-India leakage observed at any
  offset.
- ``q`` (keyword) genuinely narrows server-side (0 results for a nonsense
  token; 20/591 for ".NET"; 84/591 for "python"; 51/591 for "C#"; 591/591
  for an empty query) -- unlike Dover's tenant, this one does NOT
  over-broadly full-text-match the whole company blurb. Still ignored
  here anyway (see below) for efficiency: this repo's default keyword list
  has 10 entries, and re-paginating a ~600-job board 10 times per company
  per run is wasteful when caching the whole India pool once and letting
  matcher.py's own title_family/skill filters do the real work is exactly
  as accurate and far cheaper -- the same "cache-once" reasoning already
  used for UBS/Deutsche Bank/Persistent/PepsiCo/HealthEdge (NOT Dover's
  reasoning, which ignores keywords because they under-match; here they
  would work fine, they're just unnecessary overhead at this pool size).
- Page size is a fixed 25/page; ``startrow=N`` pagination terminates
  cleanly with zero ``<tr class="data-row">`` rows once N exceeds the true
  total (confirmed at startrow=600 on a 591-job pool) -- no
  wraparound-past-total quirk here (unlike the UBS/MUFG/Nvidia/Pfizer/
  Walmart family of Workday tenants).

Detail pages: ``<span class="jobdescription">`` holds the full JD (same
selector as Dover) and ``<meta itemprop="datePosted" content="Thu Aug 13
02:00:00 UTC 2026">``.

Location handling: unlike Dover's "City, IN" (which never says India),
Birlasoft's own location strings already start with the literal word
"INDIA" (e.g. "INDIA - BENGALURU - HP, IN", "INDIA-COIMBATORE-BIRLASOFT
OFFICE, IN"), so ``is_india_job()``'s plain "india" substring check works
with no fetcher-side augmentation needed. Only cosmetic cleanup is done
here (trailing ", IN" -> ", India") for nicer alert text. One real,
pre-existing gap found and worked around at the config level, not the
fetcher level: Birlasoft has a real Coimbatore office
("INDIA-COIMBATORE-BIRLASOFT OFFICE, IN") -- genuinely Tamil Nadu, but the
location text never says "Tamil Nadu" or "Chennai", the same leak class
already flagged for Eurofins/State Street/HealthEdge/Razorpay. Handled the
same way those companies handle it: this company's config.yaml block
overrides exclude_locations to add "Coimbatore" explicitly rather than
patching the shared default list or the fetcher.

Confirmed via direct API probing (2026-09-06): ~591 India postings across
Bengaluru/Noida/Hyderabad/Mumbai/Pune/Chennai/Coimbatore/Delhi/Mahape, with
real signal for both tracks -- explicit "Dotnet Fullstack Developer" /
"Dot Net Full stack Developer" / "MERN Full Stack Developer" titles for
`.NET / C#`, and "Generative AI Developer" / "GEN AI Lead Developer" /
"Technical Specialist-Python AI" titles for `AI / ML / Python` (see the
onboarding report for exact match counts after matcher.py's full filter
chain).
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://jobs.birlasoft.com"
_SEARCH_URL = f"{_BASE_URL}/search/"
_PAGE_SIZE = 25  # J2W always returns 25 per page; not configurable
_MAX_PAGES = 60  # safety ceiling comfortably above the current ~591-job pool

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
    """Raised on 429 or persistent connection failure from Birlasoft's J2W site."""


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_search_date(raw: str) -> str:
    """Convert 'Aug 13, 2026' (search result) to '2026-08-13'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_detail_date(raw: str) -> str:
    """Convert 'Thu Aug 13 02:00:00 UTC 2026' (meta tag) to '2026-08-13'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S UTC %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalise_location(raw: str) -> str:
    """'INDIA - BENGALURU - HP, IN' -> 'INDIA - BENGALURU - HP, India'.

    The raw text already contains the literal word "INDIA", so
    ``is_india_job()`` works unmodified -- this only tidies the trailing
    ISO code for nicer alert text, with a defensive fallback in case a
    future posting's location text ever omits "India" outright.
    """
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
                raise RateLimitError("Birlasoft: 429 rate-limited during cache fill")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Birlasoft cache fill failed: {exc}") from exc
    raise RateLimitError(f"Birlasoft cache fill: no response -- {last_exc}")


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
            time.sleep(0.15)

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
            # "/job/Pipariya-Shahnai%2C-India-Sr-Lead-SAP-BASIS-INDI/56972244/"
            # -> "56972244"
            job_id = href.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit() or job_id in seen_ids:
                continue

            loc_cell = row.select_one("td.colLocation.hidden-phone span.jobLocation")
            if not loc_cell:
                loc_cell = row.select_one("span.jobLocation")
            loc_text = html_mod.unescape(loc_cell.get_text(strip=True)) if loc_cell else ""
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
    print(f"[Birlasoft] Cache filled: {len(collected)} India jobs")


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
    """Return a page of Birlasoft India jobs.

    Keywords are ignored (see module docstring) -- the whole India pool
    (~591 jobs) is cached once and matcher.py's shared title/skill filters
    do the real work, even though the underlying ``q`` param does
    genuinely filter server-side.
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
    <meta itemprop="datePosted" content="Thu Aug 13 02:00:00 UTC 2026">.
    """
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Birlasoft description: 429 rate-limited for {application_url}")
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
