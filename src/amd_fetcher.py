"""
AMD (Advanced Micro Devices) job fetcher — iCIMS "Jibe" REST API (careers.amd.com).

ATS identification (Step 1, verified live 2026-09-13, not guessed): AMD's
careers page (careers.amd.com/careers-home/jobs) embeds
``data-jibe-search-version`` in its `<html>` tag — the same iCIMS "Jibe
Careers Site Builder" front end already confirmed at PepsiCo
(pepsico_fetcher.py), Schneider Electric, HealthEdge, and SITA in this repo.
``GET https://careers.amd.com/api/jobs`` is a plain, unauthenticated REST
endpoint returning the identical response shape as PepsiCo's tenant
(``jobs[].data`` with ``req_id``/``title``/``full_location``/``description``/
``qualifications``/``responsibilities``/``apply_url``/``ats_code: "icims"``),
full HTML descriptions inline — no per-job detail fetch needed.

Both ``location`` and ``keywords`` are genuinely applied server-side
(confirmed empirically: ``location=India`` narrows the global pool to 192 of
several thousand; ``keywords=`` values change ``totalCount``). Unlike
PepsiCo, though, this tenant's ``offset`` parameter is *also* broken but in a
different way — not "ignored" (Pepsico/UBS/Deutsche Bank pattern) but
"silently replays page 1 regardless of value" (confirmed: offset=0 and
offset=100 return byte-identical job lists). The real pagination mechanism
on this tenant is a 1-indexed ``page`` query param instead (confirmed:
``page=2`` returns a disjoint 92-job set covering the remaining part of the
192-job India total) — a new pagination shape for this ATS family, worth
remembering if another Jibe/iCIMS tenant is added later. ``limit`` hard-caps
at 100 (values above return HTTP 422).

AMD's India pool (192 jobs at verification time, all Hyderabad) is
overwhelmingly ASIC/RTL/verification/embedded/physical-design hardware
roles, as expected for a chip-design GCC — but a genuine software/AI track
does exist alongside it: "Lead Machine Learning Engineer" (explicitly
mentions "large language model" and Python in its JD body — a real
``AI / ML / Python`` primary-skill match), "AI Engineer – Model Optimization
& Acceleration", "System Software Engineer - Linux Kernel", and several
"Software Development Engineer"/"Software Engineer - Developer & Build
Experience" titles. Not force-matched — most of these titles fail
`exclude_terms` (embedded/firmware) or simply don't mention a hard
`primary_skills` term in their JD, same "confirmed-zero/low-is-real" class as
Micron/TI/Genpact — but the "Lead Machine Learning Engineer" match confirms
this is a genuine, non-trivial software-track presence worth tracking, not a
purely-hardware board like a from-scratch skip would require.

Since keyword genuinely narrows results server-side but the full unfiltered
pool is small enough to cache in two page requests, this fetcher ignores the
configured keyword and caches the complete India pool once (see
``_IGNORES_KEYWORDS`` registration) — same "cache once, slice locally"
discipline as PepsiCo/UBS/Deutsche Bank, just triggered by a different
pagination bug shape.
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://careers.amd.com"
_SEARCH_URL = f"{_BASE_URL}/api/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{_BASE_URL}/careers-home/jobs",
}

_MAX_PAGE_SIZE = 100  # server hard-caps `limit`; >100 returns HTTP 422
_MAX_PAGES = 20  # safety cap

_cache: list[dict[str, str]] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled = False


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    return raw[:10] if raw else ""


def _location_str(j: dict) -> str:
    loc = (j.get("full_location") or j.get("short_location") or "").strip()
    if loc:
        return loc
    city = (j.get("city") or "").strip()
    country = (j.get("country") or "").strip()
    if city or country:
        return f"{city}, {country}".strip(", ")
    return ""


def _fill_cache(timeout: int) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True  # set before the fetch loop — avoid a retry storm on failure

    for page in range(1, _MAX_PAGES + 1):
        params = {"location": "India", "limit": _MAX_PAGE_SIZE, "page": page}
        r = None
        for attempt in range(3):
            try:
                r = requests.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)
                if r.status_code == 429:
                    raise RateLimitError(f"429 rate-limited on attempt {attempt + 1}")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise RateLimitError(
                        f"AMD search failed after 3 attempts (page={page}): {exc}"
                    ) from exc
                time.sleep(2 ** attempt)

        raw_jobs = r.json().get("jobs", [])
        if not raw_jobs:
            break

        for item in raw_jobs:
            j = item.get("data", {})
            job_id = str(j.get("req_id") or j.get("slug") or "")
            if not job_id:
                continue

            title = (j.get("title") or "").strip()
            loc = _location_str(j)
            posting_date = _parse_date(j.get("posted_date", ""))
            apply_url = j.get("apply_url") or f"{_BASE_URL}/jobs/{job_id}"

            raw_text = " ".join(
                part
                for part in (
                    j.get("description", ""),
                    j.get("qualifications", ""),
                    j.get("responsibilities", ""),
                )
                if part
            )
            description = _strip_html(raw_text)
            _desc_cache[apply_url] = (description, posting_date)

            _cache.append({
                "id": job_id,
                "title": title,
                "location": loc,
                "posting_date": posting_date,
                "application_url": apply_url,
            })

        if len(raw_jobs) < _MAX_PAGE_SIZE:
            break


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a slice of AMD's cached India job pool.

    Keyword is ignored (the full India pool is small enough to cache once —
    see module docstring); location/sort_by are accepted for interface
    compatibility but unused beyond the initial India-scoped fetch.
    """
    _fill_cache(timeout)
    return _cache[start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    for attempt in range(3):
        try:
            r = requests.get(
                application_url, headers={**_HEADERS, "Accept": "text/html"}, timeout=timeout
            )
            if r.status_code == 429:
                raise RateLimitError(f"429 on {application_url}")
            r.raise_for_status()
            text = _strip_html(r.text)
            result = (text, "")
            _desc_cache[application_url] = result
            return result
        except RateLimitError:
            raise
        except Exception:
            if attempt == 2:
                return "", ""
            time.sleep(2 ** attempt)

    return "", ""
