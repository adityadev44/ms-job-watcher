"""
PolicyBazaar (Policybazaar.com / PB Fintech) job fetcher — Trakstar Hire ATS.

ATS discovery (live, 2026-08-31):
  - www.policybazaar.com/careers/ is a static marketing/lead-gen page (insurance
    sales/consultant recruitment funnel) with NO structured job data at all —
    four generic role *categories* ("Associate Sales Consultant", "Careers in
    Technology", ...) each sharing one "Apply now" popup that just opens a
    resume-upload form. No job IDs, no per-posting location/date, nothing a
    fetch_jobs() polling loop could dedupe against. Not usable as a source.
  - The real, structured company-branded job board lives on a separate ATS
    tenant: https://policybazaar.hire.trakstar.com/ — "Trakstar Hire" (the
    modern rebrand of the older "RecruiterBox" ATS; RSS/HTML still carry the
    `recruiterbox.com` namespace/domain internally). This is NOT one of the
    ATS vendors already in PLAYBOOK's "Common ATS vendors" table (not
    Workday/Greenhouse/Lever/SmartRecruiters/RippleHire/Avature/MyNextHire/
    Oracle HCM CE/SAP SuccessFactors/iCIMS/Taleo/Darwinbox/Zwayam/Algolia/
    Eightfold) — a genuinely new vendor for this repo, confirmed via a probe
    of Greenhouse/Lever/SmartRecruiters vanity slugs for "policybazaar" and
    "pbfintech" (all 404/redirect-to-generic-landing, ruling those out) and a
    direct hit resolving live with fresh session cookies + a working
    reCAPTCHA/Google-Maps key wired to the "Policybazaar" brand.
  - Server-rendered HTML at the tenant root lists every open job as a plain
    `<div class="js-careers-page-job-list-item" data-href="/jobs/{slug}/">`
    row (title, city/state/country spans) — no JS execution required. The
    tenant also exposes a full-board RSS feed at `/jobfeeds/Policybazaar`
    (same `recruiterbox.com/rss/job/` XML namespace used by every Trakstar
    Hire tenant checked) that bundles the FULL HTML job description inline
    for every currently-open job in one request — no per-job detail fetch
    needed, so the whole board is cached once per process (same cache-once
    idiom as cognizant_fetcher.py's RSS approach / lenskart_fetcher.py).

Response shape (RSS <item>): `title`, `link` (`.../jobs/{slug}`, used to
derive both the job id and the application_url), `description` (HTML,
entity-escaped — strip tags after XML-unescaping), and three fields in the
`job:` (recruiterbox.com/rss/job/) namespace: `locationCity`, `locationState`,
`locationCountry`. These are combined into "City, State, Country" — since
`locationCountry` is an explicit, always-literal "India" string for India
postings (verified — unlike most other companies' messy free-text location
fields), no city-whitelist normalisation hack is needed here at all.

Quirk — RSS `pubDate` is NOT a real per-job posting date. Cross-checked
against two other live Trakstar Hire tenants (Zoho, Swiggy — both companies
that have since moved to other ATSes per this repo's existing fetchers, but
left this legacy subdomain live): every `<item>`'s `pubDate` is byte-identical
to the channel's own `<lastBuildDate>`, and that value differs only per
*tenant* (fixed at whatever date the tenant was provisioned — PolicyBazaar's
and Zoho's are both the literal string "Fri, 08 Nov 2013", Swiggy's is
"Wed, 16 Dec 2015") — two completely different job titles on the same Zoho
tenant ("Developer" and "Designer") share the exact same pubDate, proving it
is a fixed platform/template default wired to tenant creation, not a real
per-job timestamp. Treated the same as IBM's "no posting-date field exposed
anywhere" case in PLAYBOOK: `posting_date` is left as `""` rather than
surfacing a fake, misleading 2013 date on a job that may have opened days ago.

Coverage reality check (live, 2026-08-31): PolicyBazaar/PB Fintech is
genuinely and actively hiring software engineers right now (confirmed via
several live LinkedIn postings — "Software Engineer", "Lead Software
Engineer", "Technical Associate" — all within the past few weeks), but NONE
of that flows through this Trakstar Hire board: it currently lists exactly
one open requisition system-wide ("Assistant Manager - Social Media
Marketing", Gurgaon), not an engineering role. This mirrors the Zerodha/ING
precedent in PLAYBOOK ("Key Bugs"/"Batch Onboarding Wave 1") — a correctly
working, live, unprotected data source that currently yields ~0 matches is a
real fact about the company's hiring channel, not a fetcher defect. Any
future engineering req PolicyBazaar posts to this board will flow through
automatically; the company's LinkedIn/Naukri-only tech postings (which never
touch a public API) are out of scope for this fetcher, same as every other
company in this repo only integrates its own ATS.

Keyword/location params are accepted for interface compatibility but ignored:
a live `?q=` probe against the tenant's search returned an identical
`total_jobs` count regardless of query string, confirming filtering happens
client-side in the browser after the page loads with the full unfiltered set
(same conclusion already reached for several other companies' boards).
"""
from __future__ import annotations

import html as html_mod
import re
import time
import xml.etree.ElementTree as ET

import requests

_TENANT = "policybazaar"
_RSS_URL = f"https://{_TENANT}.hire.trakstar.com/jobfeeds/Policybazaar"
_JOB_DETAIL_BASE = f"https://{_TENANT}.hire.trakstar.com/jobs"

_JOB_NS = "https://recruiterbox.com/rss/job/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/xml,application/xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure fetching the RSS feed."""


# Module-level cache: the whole board (title/location/description) is fetched
# once per process via one RSS request, and re-sliced for every subsequent
# keyword/page call — same idiom as cognizant_fetcher.py / lenskart_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _slug_from_url(url: str) -> str:
    """RSS link/guid is '.../jobs/{slug}' (no trailing slash) -> take the slug."""
    path = (url or "").split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def _job_ns_text(item, field: str) -> str:
    elem = item.find(f"{{{_JOB_NS}}}{field}")
    return (elem.text or "").strip() if elem is not None else ""


def _build_location(city: str, state: str, country: str) -> str:
    parts = [p for p in (city.strip(), state.strip(), country.strip()) if p]
    return ", ".join(parts)


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire PolicyBazaar (Trakstar Hire) RSS board once and cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_RSS_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("PolicyBazaar: 429 rate-limited during RSS fetch")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"PolicyBazaar RSS fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"PolicyBazaar RSS fetch: no response — {last_exc}")

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        raise RateLimitError(f"PolicyBazaar RSS XML parse error: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        _job_cache[:] = []
        print("[PolicyBazaar] Cache filled: 0 total jobs (no <channel> in RSS)")
        return

    collected: list[dict] = []
    for item in channel.findall("item"):
        title_elem = item.find("title")
        title = (title_elem.text or "").strip() if title_elem is not None else ""
        link_elem = item.find("link")
        link = (link_elem.text or "").strip() if link_elem is not None else ""
        if not (title and link):
            continue

        slug = _slug_from_url(link)
        if not slug:
            continue
        job_id = slug
        application_url = f"{_JOB_DETAIL_BASE}/{slug}/"

        city = _job_ns_text(item, "locationCity")
        state = _job_ns_text(item, "locationState")
        country = _job_ns_text(item, "locationCountry")
        location = _build_location(city, state, country)

        # pubDate is a fixed per-tenant template default, not a real per-job
        # posting date (see module docstring) — deliberately not surfaced.
        posting_date = ""

        desc_elem = item.find("description")
        raw_desc = (desc_elem.text or "") if desc_elem is not None else ""
        description = _strip_html(raw_desc)
        _desc_cache[application_url] = description

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    _job_cache[:] = collected
    print(f"[PolicyBazaar] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of PolicyBazaar jobs from the cached full RSS board.

    keyword/location are accepted for interface compatibility but ignored:
    a live `?q=` probe against the tenant's own search page returned an
    identical job count regardless of the query string, confirming
    keyword/location filtering happens client-side in the browser, not
    server-side — the RSS feed always returns every currently-open job.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single PolicyBazaar job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() — the
    RSS feed already includes each job's full HTML description, so no
    separate per-job HTTP request is made. posting_date is always "" (see
    module docstring — RSS pubDate is a fixed template default, not real).
    """
    _fill_cache(timeout=timeout)

    description = _desc_cache.get(application_url, "")
    posting_date = ""
    for job in _job_cache:
        if job["application_url"] == application_url:
            posting_date = job["posting_date"]
            break

    return description, posting_date
