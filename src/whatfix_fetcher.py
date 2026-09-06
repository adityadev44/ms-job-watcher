"""Fetches Whatfix job listings via the Trakstar Hire (RecruiterBox) RSS feed.

ATS discovery (live, 2026-09-06): whatfix.com/careers/ ships a deferred
theme JS file (`WhatfixAPI.js`) whose career-search form fires
`fetch("https://jsapi.recruiterbox.com/v1/openings?client_name=whatfix101" +
filters)` -- confirmed by reading the raw JS source, not guessed. That
`client_name` ("whatfix101") identifies the company's Trakstar Hire
("RecruiterBox" is the platform's legacy/internal name -- same vendor as
policybazaar_fetcher.py/zomato_fetcher.py's dead tenant) tenant. Trakstar
Hire tenants also expose a full-board RSS feed bundling title/location/
description/pubDate for every currently-open posting in one request:

    GET https://whatfix101.hire.trakstar.com/jobfeeds/whatfix101

Verified live: 200 OK, 31 total postings, 23 explicitly location-tagged
"India" (all Bengaluru) plus several more Bengaluru postings whose
structured `job:locationCountry` field is simply empty (see below) -- no
`?q=`/keyword param exists on this feed at all, so (same as PolicyBazaar)
the whole board is fetched once per process and cached; the shared matcher
does the real title/skill/India filtering.

Quirk -- unlike PolicyBazaar's Trakstar Hire tenant (whose RSS `pubDate` is
a fixed, fake per-tenant-creation date), Whatfix's `pubDate` values genuinely
differ per job and match the live JobPosting `datePosted` schema.org value
sampled from a job's detail page (cross-checked one posting: RSS pubDate
"2026-09-01" == on-page `datePosted "2026-09-01"`) -- treated as a real,
trustworthy per-job posting date here.

Quirk -- `job:locationCountry`/`job:locationState` are missing (empty) for
roughly a third of postings even though `job:locationCity` is always
"Bengaluru" (Whatfix's HQ) for India roles; the company's only other office
seen live is Sydney/San Jose for a couple of North-America GTM roles. Since
Whatfix has no other international engineering site, any posting whose city
is Bengaluru/Bangalore/Gurgaon/Gurugram/Hyderabad is treated as India even
when the structured country field is blank (same "append India for a known
office city" idiom used elsewhere in this repo for Fidelity/Marsh McLennan/
Barclays) -- a posting with a genuinely non-India city and no country field
is left as-is and naturally excluded by the shared India filter.

require_tech_in_description is NOT enabled -- live India titles include
direct, specific engineering roles ("Software Engineer - E6 - Backend",
"Forward Deployed Engineer", "Lead AI Forward Deployed Engineer") rather
than generic IT-services level bands.
"""
from __future__ import annotations

import html as html_mod
import re
import time
import xml.etree.ElementTree as ET

import requests

_TENANT = "whatfix101"
_RSS_URL = f"https://{_TENANT}.hire.trakstar.com/jobfeeds/{_TENANT}"
_JOB_DETAIL_BASE = f"https://{_TENANT}.hire.trakstar.com/jobs"

_JOB_NS = "https://recruiterbox.com/rss/job/"

_INDIA_CITY_HINTS = ("bengaluru", "bangalore", "gurgaon", "gurugram", "hyderabad", "noida")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/xml,application/xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure fetching the RSS feed."""


# Module-level cache: the whole board (title/location/description/date) is
# fetched once per process via one RSS request, and re-sliced for every
# subsequent keyword/page call -- same idiom as policybazaar_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _slug_from_url(url: str) -> str:
    path = (url or "").split("?", 1)[0]
    return path.rstrip("/").rsplit("/", 1)[-1]


def _job_ns_text(item, field: str) -> str:
    elem = item.find(f"{{{_JOB_NS}}}{field}")
    return (elem.text or "").strip() if elem is not None else ""


def _build_location(city: str, state: str, country: str) -> str:
    if country:
        parts = [p for p in (city, state, country) if p]
        return ", ".join(parts)
    if city:
        # locationCity is occasionally itself a full "City, State, India"
        # string (structured state/country left blank) -- don't double up.
        if "india" in city.lower():
            return city
        if any(hint in city.lower() for hint in _INDIA_CITY_HINTS):
            return f"{city}, India"
    return city


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Whatfix (Trakstar Hire) RSS board once and cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent lesson -- see PLAYBOOK "Key Bugs").
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
                raise RateLimitError("Whatfix: 429 rate-limited during RSS fetch")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Whatfix RSS fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Whatfix RSS fetch: no response -- {last_exc}")

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        raise RateLimitError(f"Whatfix RSS XML parse error: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        _job_cache[:] = []
        print("[Whatfix] Cache filled: 0 total jobs (no <channel> in RSS)")
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
        application_url = f"{_JOB_DETAIL_BASE}/{slug}/"

        city = _job_ns_text(item, "locationCity")
        state = _job_ns_text(item, "locationState")
        country = _job_ns_text(item, "locationCountry")
        location = _build_location(city, state, country)

        pub_elem = item.find("pubDate")
        posting_date = ""
        if pub_elem is not None and pub_elem.text:
            # RFC-822 "Tue, 01 Sep 2026 00:00:00 +0530" -> "2026-09-01"
            m = re.match(r"\w+, (\d{2}) (\w{3}) (\d{4})", pub_elem.text.strip())
            if m:
                try:
                    from datetime import datetime
                    posting_date = datetime.strptime(
                        " ".join(m.groups()), "%d %b %Y"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    posting_date = ""

        desc_elem = item.find("description")
        raw_desc = (desc_elem.text or "") if desc_elem is not None else ""
        description = _strip_html(raw_desc)
        _desc_cache[application_url] = description

        collected.append({
            "id": slug,
            "title": title,
            "location": location,
            "posting_date": posting_date,
            "application_url": application_url,
        })

    _job_cache[:] = collected
    print(f"[Whatfix] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Whatfix jobs from the cached full RSS board.

    keyword/location are accepted for interface compatibility but ignored --
    this Trakstar Hire tenant's RSS feed has no keyword/location param at
    all and always returns every currently-open job; the shared matcher does
    the real title/skill/India filtering.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start: start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Whatfix job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() --
    the RSS feed already includes each job's full HTML description, so no
    separate per-job HTTP request is made.
    """
    _fill_cache(timeout=timeout)

    description = _desc_cache.get(application_url, "")
    posting_date = ""
    for job in _job_cache:
        if job["application_url"] == application_url:
            posting_date = job["posting_date"]
            break

    return description, posting_date
