"""Procter & Gamble job fetcher — Phenom People CMS (pgcareers.com).

ATS identification (Step 1, verified live 2026-09-13): www.pgcareers.com is a
Phenom People CareerConnect tenant (`cdn.phenompeople.com/CareerConnectResources/
PGBPGNGLOBAL/...` asset paths, company code `PGBPGNGLOBAL`, `id="_PCM"` on the
`<html>` tag — the exact same platform/markup fingerprint already documented
for GSK/Godrej/Morningstar in this repo). Its `/api/apply/v2/jobs` and
`/widgets` endpoints both reject plain unauthenticated requests
(`{"status":"failure","errorCode":null,"errorMsg":"Tenant not identified",...}`)
the same way GSK's tenant does — this fetcher uses the identical two-step
sitemap workaround already proven for gsk_fetcher.py/morningstar_fetcher.py
rather than re-attempting the JS-only search API:

  1. Fetch the sitemap index (`pgcareers.com/sitemap.xml` redirects 303 to
     `pgcareers.com/global/en/sitemap_index.xml`, which lists two child
     sitemaps `sitemap1.xml`/`sitemap2.xml` covering ~841 live URLs total,
     of which ~140 are `/global/en/job/{reqId}/{slug}` job postings — the
     rest are static/marketing pages, filtered out by URL shape).
  2. Fetch each job page once and read its `JobPosting` JSON-LD block for
     title, location, date, and description (a second, unrelated JSON-LD
     block of a different `@type` sometimes appears on the same page —
     skipped by the `@type` check, same as GSK).

P&G's site (unlike GSK's) fronts every request with a CloudFront-backed WAF
that returns a plain-text "403 ERROR ... Request blocked" CloudFront error
page (not JSON, not the normal Phenom 403) if too many job-detail requests
land in a short burst — confirmed live: a 20-way-parallel scan of the full
~140-job pool triggered a temporary blanket block on every subsequent
`/global/en/job/*` request (but not on the homepage) for several minutes.
`_fill_cache` therefore fetches job pages strictly sequentially with a
polite inter-request delay, not in parallel, and a bare 403 on a job page is
treated as "skip this one" rather than a fatal error so one WAF hiccup does
not zero out the whole run.

Confirmed live 2026-09-13: P&G genuinely runs an India tech/GCC pipeline —
28 of ~140 currently open global postings are India-based (Mumbai/Bangalore
GCC), including real software-engineering titles ("Senior Data Engineer" x2,
"DevOps Engineer", "Information Security Engineer", "Smart Automations
Platform Engineer", "Director-IT-Solution-Architecture", "Data Scientist",
"Digital Product Manager" x2, plus IT internships) alongside the expected
FMCG-majority mix (Sales/Marketing/R&D/Finance/HR/Legal) — this is a real,
current fact about P&G's India hiring, not assumed from precedent.

India detection: JSON-LD `jobLocation.address.addressCountry == "India"`
(confirmed clean/consistent on this tenant, same as GSK — no city-only or
state-only rows observed).
"""
from __future__ import annotations

import html as _html_mod
import json
import re
import time

import requests

_SITEMAP_INDEX = "https://www.pgcareers.com/sitemap.xml"
_SITEMAP_FALLBACK = [
    "https://www.pgcareers.com/global/en/sitemap1.xml",
    "https://www.pgcareers.com/global/en/sitemap2.xml",
]
_JOB_URL_RE = re.compile(r"https://www\.pgcareers\.com/global/en/job/[\w.-]+/[^\s<]*")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Polite delay between sequential job-page fetches -- P&G's CloudFront WAF
# temporarily blanket-blocks the /job/ path after a parallel burst (see
# module docstring); sequential + a small delay avoids re-triggering it.
_DETAIL_DELAY = 0.3

# Module-level cache -- filled on first fetch_jobs() call.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled = False


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_job_page(html_text: str, url: str) -> dict | None:
    """Extract job data from a Phenom People job page via JSON-LD.

    Returns a job dict (with an internal ``_description`` key) or None if
    the posting is closed/unparseable or not India-based.
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
        if d.get("@type") != "JobPosting":
            continue

        addr = d.get("jobLocation", {}).get("address", {})
        country = addr.get("addressCountry", "")
        city = addr.get("addressLocality", "")

        if "india" not in country.lower():
            return None

        job_id = d.get("identifier", {}).get("value", "")
        title = d.get("title", "").strip()
        date = (d.get("datePosted", "") or "")[:10]
        location_str = f"{city}, India" if city else "India"
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
    """Fetch the sitemap index + every job page once, cache India jobs."""
    global _cache, _desc_cache

    job_urls: list[str] = []
    try:
        r = requests.get(_SITEMAP_INDEX, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        child_sitemaps = re.findall(r"<loc>([^<]+)</loc>", r.text)
        if not child_sitemaps:
            child_sitemaps = _SITEMAP_FALLBACK
    except Exception:
        child_sitemaps = _SITEMAP_FALLBACK

    for sm_url in child_sitemaps:
        for attempt in range(3):
            try:
                rs = requests.get(sm_url, headers=_HEADERS, timeout=timeout)
                if rs.status_code == 429:
                    raise RateLimitError(f"429 on {sm_url}")
                rs.raise_for_status()
                job_urls.extend(_JOB_URL_RE.findall(rs.text))
                break
            except RateLimitError:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise RateLimitError(f"P&G sitemap fetch failed: {exc}") from exc
                time.sleep(2 ** attempt)

    # Sequential, not parallel -- see module docstring on the CloudFront WAF.
    for url in dict.fromkeys(job_urls):  # de-dupe, preserve order
        time.sleep(_DETAIL_DELAY)
        try:
            rj = requests.get(url, headers=_HEADERS, timeout=timeout)
            if rj.status_code != 200:
                continue
            job = _parse_job_page(rj.text, url)
            if job:
                desc = job.pop("_description", "")
                _cache.append(job)
                _desc_cache[url] = (desc, job["posting_date"])
        except Exception:
            continue


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of P&G India jobs.

    All keywords produce the same full India job set (cached after first
    call, same pattern as GSK/Morningstar). Pagination via start/num slices
    the cache list.
    """
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True  # set before try to avoid retry storm on failure
        _fill_cache(timeout=timeout)

    return _cache[start: start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a P&G job.

    Served from the in-memory cache built during fetch_jobs(); no extra
    HTTP request is made unless the URL is missing from cache (rare).
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    job = _parse_job_page(r.text, application_url)
    if job:
        desc = job.get("_description", "")
        date = job.get("posting_date", "")
        _desc_cache[application_url] = (desc, date)
        return desc, date
    return "", ""
