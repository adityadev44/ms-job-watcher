"""Fetches KPIT Technologies (www.kpit.com) India job listings.

KPIT's public careers page is a WordPress site that server-renders all India
job openings in a single HTML page when `?country=India&show_all=1` is passed.
The underlying ATS is TalentOjo (talentojo.kpit.com) — a JavaScript SPA that
cannot be scraped without a browser — but the WordPress listing page carries
enough metadata (title, city, skills text) to feed the matcher.

Key facts confirmed live 2026-09-06:
  - 28 India jobs visible at /job-listing/?country=India&show_all=1 (no
    pagination needed; "show_all=1" bypasses the default 6-item short list).
  - Each job card is `div.search_job_box` containing `<h3>` title,
    `span.job-loaction` (sic) city, and `p.skills-txt` skills.
  - Apply link points to `https://talentojo.kpit.com/tojo/app/job-apply/
    #/Career%20Portal/{ID}` — the numeric ID after the fragment is used as
    the job's unique identifier.
  - No posting date is server-rendered on the listing page; the field is
    left empty in fetch_jobs() and the matcher treats absent dates as oldest.
  - Keywords are NOT sent server-side; this fetcher ignores the `keyword`
    argument and caches the full India pool once per process run (same pattern
    as Mastek/Wipro/Nomura). The shared matcher's title/skill checks do the
    real narrowing.
  - Descriptions are the skills-text line already in the listing; the full JD
    lives inside TalentOjo's SPA (auth-gated, unfetchable without a browser),
    so `fetch_job_description` raises NotImplementedError and the job is kept
    as "Unverified" (same approach as every other inline-description company).
    The coordinator should add "kpit" to _INLINE_DESCRIPTIONS in the registry.

KPIT is an automotive/embedded-software engineering company; most titles will
not match the default title_family (no "software engineer", "machine learning
engineer" in most auto-domain roles) — low match count expected.
"""
from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

_LISTING_URL = "https://www.kpit.com/job-listing/"
_TOJO_APPLY_BASE = "https://talentojo.kpit.com/tojo/app/job-apply/#/Career%20Portal/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


class RateLimitError(Exception):
    """Raised on 429 or persistent connection failure from KPIT WordPress."""


# Module-level cache: entire India pool, fetched once per process run.
_job_cache: list[dict] = []
_cache_filled: bool = False


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the single KPIT India listing page and cache all jobs."""
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _LISTING_URL,
                params={"country": "India", "show_all": "1"},
                headers=_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("KPIT: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"KPIT fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"KPIT fetch: no response — {last_exc}")

    soup = BeautifulSoup(r.text, "html.parser")
    jobs: list[dict] = []
    seen_ids: set[str] = set()

    for box in soup.find_all("div", class_="search_job_box"):
        title_el = box.find("h3")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        loc_el = box.find("span", class_="job-loaction")
        city = loc_el.get_text(strip=True) if loc_el else ""
        location = f"{city}, India" if city else "India"

        skills_el = box.find("p", class_="skills-txt")
        skills = skills_el.get_text(" ", strip=True) if skills_el else ""

        # Apply link → TalentOjo SPA fragment URL
        apply_a = box.find("a", href=re.compile(r"talentojo", re.I))
        if not apply_a:
            # Fallback: any "Apply" link
            apply_a = box.find("a", string=re.compile(r"apply", re.I))
        href = apply_a.get("href", "") if apply_a else ""

        m = re.search(r"Career%20Portal/(\d+)", href)
        job_id = m.group(1) if m else ""
        if not job_id or job_id in seen_ids:
            continue

        seen_ids.add(job_id)
        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",
            "description": skills,
            "application_url": f"{_TOJO_APPLY_BASE}{job_id}",
        })

    _job_cache[:] = jobs
    print(f"[KPIT] Cache filled: {len(jobs)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of KPIT India jobs.

    Keyword and location arguments are accepted but ignored — the KPIT
    WordPress page always returns the full India pool; the shared matcher's
    title/skill filters do the real narrowing.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """KPIT descriptions are the skills text already returned by fetch_jobs."""
    raise NotImplementedError("descriptions are inline")
