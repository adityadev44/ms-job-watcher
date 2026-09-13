"""Fetches Godrej Industries Group's job listings via its Phenom People
CareerConnect tenant (careers.godrejindustries.com).

ATS discovery (live, 2026-09-13): godrej.com/careers is a bare landing page
that only links out to two post-2024-restructuring group portals --
Godrej Enterprises Group (godrejenterprises.com, the industrial/auto/
appliances/security side -- its own custom "CareerWEB" ASP-style ATS at
careeropportunities.godrejenterprises.com, confirmed live to have only 13
open India postings today, all sales/manufacturing/ops roles, zero SDE-
relevant) and Godrej Industries Group (careers.godrejindustries.com --
covers GCPL/Godrej Consumer Products, Godrej Properties, Godrej Capital,
Godrej Agrovet/Chemicals, Godrej Ventures, etc. under one shared tenant).
Godrej Industries Group is the entity onboarded here: it has genuine,
current SDE-track India postings (Full Stack Developer, Java+Angular/React
Fullstack Developer, Senior DevOps Engineer, DevOps/MLOps Engineer with
real GenAI/LLMOps/RAG-pipeline responsibilities, Senior Executive - Data
Engineer, Assistant Manager - Data Scientist, "SDE II") -- confirmed live
via a real sitemap scan, not guessed from a company-name vibe.

Same Phenom People CareerConnect shape as ``morningstar_fetcher.py`` and
the Evernorth/GE Aerospace/GE HealthCare/Cisco/Disney family documented in
this repo's playbook: a `PLAY_SESSION` JWT + CSRF-token session-gated
`/widgets` search API that returns `{"status":"failure"}` to plain
``requests`` (confirmed live), and a server-rendered `/search-results` page
whose own initial payload is an empty `"jobs":[]` shell (client-side JS
fetches the real list after page load) -- so, same fix as Morningstar:
walk the tenant's own `sitemap.xml` for the full list of `/job/{id}/{slug}`
URLs (404 confirmed live, no pagination needed -- `/in/en/sitemap.xml` and
the business-unit-scoped `/in/en/godrejindustriesgroup/sitemap.xml` are
byte-identical), then fetch each job page once and extract the embedded
JSON-LD `JobPosting` block (title, datePosted already ISO, full HTML
description, and a `jobLocation.address` object) -- no auth, no JS
execution, no separate detail API needed at all. Results are cached at
module level; `fetch_job_description` is served from that cache with a
live-fetch fallback only for a URL never seen via `fetch_jobs` in this
process.

**Load-bearing gotcha found live, not present on Morningstar's tenant**:
this tenant's `addressLocality`/`addressRegion` fields are inconsistently
authored in Devanagari (Hindi) script for some postings and plain English
for others -- e.g. one live "Full Stack Developer" posting showed
``addressLocality: "मुंबई", addressRegion: "महाराष्ट्र"`` while a
different, contemporaneous "Java + Angular/React Fullstack Developer"
posting at the same tenant showed ``addressLocality: "Mumbai",
addressRegion: "Maharashtra"`` -- confirmed this is genuinely per-posting
(likely which recruiter/HRIS record created the requisition), not a
locale/session artifact, since both pages were fetched with the identical
`/in/en/` URL and headers. ``addressCountry`` was reliably "India" in
English on every India posting sampled, so country detection is
unaffected -- but a Hindi-script city name would silently bypass
`exclude_locations`'s plain-English substring match (e.g. a Pune posting
authored as "पुणे" would never match config.yaml's "Pune" exclusion
string). Defended against here with a small Devanagari->English
transliteration table covering exactly the `default_exclude_locations`
city/state names (Pune, Chennai, Tamil Nadu, Chandigarh, Kochi, Kerala,
Trivandrum, Lucknow, Nagpur, Madurai, Kolkata, Indore, Vadodara) --
`_normalise_location` checks the raw Devanagari city string against this
table before falling back to using it verbatim, so a Hindi-authored
excluded-city posting still gets caught. Residual risk: a Hindi-authored
*non-excluded* city we have no English mapping for would still show up
under its Devanagari name in the alert location field (correctly kept,
just less readable) -- not a filtering bug, only a cosmetic one, since
`is_india_job()` will still see the reliable English "India" country
string appended.

A handful of sampled postings had no `jobLocation`/`address` block at all
(e.g. "Senior Executive - Estate") -- these are dropped rather than
defaulted to India, since Godrej Industries Group genuinely posts non-
India roles too (an "Executive - Accounts Payable LATAM" was observed live
in the same sitemap scan).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_SITEMAP_URL = "https://careers.godrejindustries.com/in/en/sitemap.xml"
_JOB_URL_PREFIX = "https://careers.godrejindustries.com/in/en/job/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Devanagari -> English map for exactly the excluded-city/state names this
# repo's config.yaml default_exclude_locations cares about (see module
# docstring -- this tenant inconsistently authors location fields in Hindi
# for some postings). Not an exhaustive Hindi gazetteer, just enough to
# keep the exclude-city guarantee intact for this specific tenant.
_DEVANAGARI_EXCLUDES = {
    "पुणे": "Pune",
    "चेन्नई": "Chennai",
    "तमिलनाडु": "Tamil Nadu",
    "चंडीगढ़": "Chandigarh",
    "कोच्चि": "Kochi",
    "केरल": "Kerala",
    "तिरुवनंतपुरम": "Trivandrum",
    "लखनऊ": "Lucknow",
    "नागपुर": "Nagpur",
    "मदुरै": "Madurai",
    "कोलकाता": "Kolkata",
    "इंदौर": "Indore",
    "वडोदरा": "Vadodara",
}
_JOB_ID_RE = re.compile(r"/job/([^/]+)/")

# Module-level cache -- filled on first fetch_jobs() call. The tenant's
# real search/`widgets` API is session-gated (see module docstring), so
# every keyword pass shares one sitemap-driven scan of the full India job
# pool, same "cache-once" idiom as morningstar_fetcher.py.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _normalise_city(raw: str) -> str:
    return _DEVANAGARI_EXCLUDES.get((raw or "").strip(), raw)


def _parse_job_page(html_text: str, url: str) -> dict | None:
    """Extract job data from a Phenom People job page via its JSON-LD block.

    Returns a job dict (with an extra ``_description`` key) or ``None`` if
    the job isn't India-based or parsing fails.
    """
    blocks = re.findall(
        r'<script[^>]+application/ld\+json[^>]*>([\s\S]*?)</script>',
        html_text,
    )
    for raw_block in blocks:
        try:
            d = json.loads(raw_block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        # Some pages emit a JSON-LD block that is a bare list (e.g. a
        # BreadcrumbList's itemListElement at the top level) rather than a
        # single object -- confirmed live to occur on at least one job page
        # during the sitemap scan. Skip anything that isn't the expected
        # single JobPosting object instead of raising on `.get`.
        if not isinstance(d, dict) or d.get("@type") != "JobPosting":
            continue

        # Multi-location postings carry jobLocation as a LIST of Place
        # objects rather than a single one -- confirmed live (a bare
        # ``.get`` on a list raises AttributeError, caught while walking
        # the full sitemap). Normalize to a list either way and treat the
        # posting as an India job if ANY listed location is India.
        raw_locations = d.get("jobLocation")
        if isinstance(raw_locations, dict):
            raw_locations = [raw_locations]
        elif not isinstance(raw_locations, list):
            raw_locations = []

        india_addrs = []
        for loc in raw_locations:
            if not isinstance(loc, dict):
                continue
            addr = loc.get("address")
            if not isinstance(addr, dict):
                continue
            if "india" in (addr.get("addressCountry", "") or "").lower():
                india_addrs.append(addr)

        if not india_addrs:
            return None  # no address at all, or a genuine non-India-only role

        addr = india_addrs[0]
        city = _normalise_city(addr.get("addressLocality", "") or "")
        region = _normalise_city(addr.get("addressRegion", "") or "")
        parts = [p for p in (city, region) if p]
        location_str = ", ".join(parts + ["India"]) if parts else "India"

        m = _JOB_ID_RE.search(url)
        job_id = m.group(1) if m else url
        title = (d.get("title") or "").strip()
        date = (d.get("datePosted") or "").strip()[:10]
        description = _strip_html(d.get("description", ""))

        if not job_id or not title:
            return None

        return {
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": date,
            "application_url": url,
            "_description": description,
        }
    return None


def _fill_cache(timeout: int = 20) -> None:
    global _cache, _desc_cache

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_SITEMAP_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError("Godrej: 429 on sitemap")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Godrej sitemap fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Godrej sitemap: no response -- {last_exc}")

    job_urls = re.findall(
        r"<loc>(" + re.escape(_JOB_URL_PREFIX) + r"[^<]+)</loc>",
        r.text,
    )

    for url in job_urls:
        time.sleep(0.15)
        try:
            rj = requests.get(url, headers=_HEADERS, timeout=timeout)
            if rj.status_code in (404, 410):
                continue
            if rj.status_code != 200:
                continue
            job = _parse_job_page(rj.text, url)
            if job:
                desc = job.pop("_description", "")
                _cache.append(job)
                _desc_cache[url] = (desc, job["posting_date"])
        except requests.RequestException:
            continue


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Godrej Industries Group India jobs.

    The tenant's own search/widgets API requires a browser-issued session
    (see module docstring), so every keyword produces the same full India
    pool -- cached once per process via a sitemap scan. ``keyword`` and
    ``location`` are accepted for contract compatibility only.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before the fill to avoid a retry storm
        _fill_cache(timeout=timeout)

    return _cache[start: start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a Godrej job.

    Served from the cache filled by ``fetch_jobs``; falls back to a live
    fetch + JSON-LD parse only if the URL was never cached in this process.
    """
    cached = _desc_cache.get(application_url)
    if cached is not None:
        return cached

    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"Godrej description: 429 for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return "", ""

    if r is None:
        return "", ""

    job = _parse_job_page(r.text, application_url)
    if job:
        desc = job.get("_description", "")
        date = job.get("posting_date", "")
        _desc_cache[application_url] = (desc, date)
        return desc, date
    return "", ""
