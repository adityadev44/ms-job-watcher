"""
Maersk job fetcher — Workday CXS REST API.

Endpoint: maersk.wd3.myworkdayjobs.com/wday/cxs/maersk/Maersk_Careers/jobs
All India location WIDs sent as a facet filter; results cached in-module so
subsequent keyword calls are free. Description fetched on demand per job via
the per-job CXS endpoint.
"""
from __future__ import annotations

import re
import time

import requests

_SEARCH_URL = "https://maersk.wd3.myworkdayjobs.com/wday/cxs/maersk/Maersk_Careers/jobs"
_DETAIL_BASE = "https://maersk.wd3.myworkdayjobs.com/wday/cxs/maersk/Maersk_Careers"
_JOB_BASE = "https://maersk.wd3.myworkdayjobs.com/Maersk_Careers"

# India location WIDs from Workday facets (covers all India offices).
# Extract via GET /wday/cxs/maersk/Maersk_Careers/jobs and inspecting
# facets[3].values[0].values where descriptor starts with "India".
_INDIA_WIDS = [
    "8df45049b43810013bfc511ded230000",  # INBLR08 - Bangalore LF Logistics
    "26c4d72049dc10009e0daf1507aa0000",  # India, Bengaluru, 560025
    "853120f5cc8a10009e388f5d2aec0000",  # India, Bengaluru, 560064
    "15350d48499210009e07c02b134d0000",  # India, Bengaluru, 562114
    "ddba4775944910009e1b192970560000",  # India, Bengaluru, 562114 (alt)
    "d8801e5af43d10009dfa3a8079340000",  # India, Bengaluru, 562123
    "26c4d72049dc10009e1c3d2fa2b50000",  # India, Chennai, 600002
    "5fb6db3471ab10009df11e5466840000",  # India, Chennai, 600032
    "d8801e5af43d10009e0f6cca3a670000",  # India, Chennai, 600116
    "7a38998ecd771001354b00da72100000",  # India, Gujarat, Mehsana
    "e27b2cd8aca310009e0d527ac7650000",  # India, Gurgaon, 122022
    "acb61e9d6f33100148509b86aa0d0000",  # India, Gurgaon, Farukh Nagar
    "d8801e5af43d10009df3a49ebd480000",  # India, Guwahati
    "d8801e5af43d10009e04f442bf430000",  # India, Haryana, Farukh Nagar
    "9de49f588a0f10009e1c35c6900a0000",  # India, Haryana, Farukh Nagar (alt)
    "7b99c86bc5751001ee77c1dad0500000",  # India, Jhajjar
    "d8801e5af43d10009e1c799413400000",  # India, Kolkata, 700017
    "614482d9d2ed1000c19c822fe2b50000",  # India, Maharashtra, Pune, 410501
    "e78dddcb583810009e1299afb33f0000",  # India, Mumbai, 400079
    "d8801e5af43d10009e1c6f60d76f0000",  # India, Mumbai, 400707
    "e27b2cd8aca310009e0bdadc1aec0000",  # India, Nagpur
    "5fb6db3471ab10009e1c4676598a0000",  # India, Pipavav
    "d8801e5af43d10009e13b84909090000",  # India, Pune, 410501
    "d8801e5af43d10009e1bfb079d6e0000",  # India, Pune, 411014
    "26c4d72049dc10009e1694dea72b0000",  # India, Sikandrabad
    "4140682c689310014db3b323f82e0000",  # India, Tamil Nadu, Kancheepuram
    "7b88c76f001e10009e153d90f32d0000",  # India, West Bengal, Kolkata
    "9cbc082ae7f910013707c12d02c50000",  # INMAA18 - Chennai Maersk India
]

_PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://www.maersk.com/careers",
}

# Module-level caches — filled once per process run.
_cache: list[dict] = []
_desc_cache: dict[str, tuple[str, str]] = {}
_cache_filled = False


class RateLimitError(Exception):
    pass


def _strip_html(raw: str) -> str:
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through all India Maersk jobs and fill _cache."""
    offset = 0
    while True:
        body = {
            "appliedFacets": {"locations": _INDIA_WIDS},
            "limit": _PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        for attempt in range(3):
            try:
                r = requests.post(_SEARCH_URL, headers=_HEADERS, json=body, timeout=timeout)
                if r.status_code == 429:
                    raise RateLimitError("429 on Maersk Workday search")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise RateLimitError(f"Maersk search failed: {exc}") from exc
                time.sleep(2 ** attempt)

        data = r.json()
        jobs = data.get("jobPostings", [])
        total = data.get("total", 0)
        if not jobs:
            break

        for j in jobs:
            ext_path = j.get("externalPath", "")
            if not ext_path:
                continue
            job_id = ext_path.rstrip("/").rsplit("/", 1)[-1]
            loc_text = j.get("locationsText", "") or ""
            # Workday shows "2 Locations" when a job is open at multiple sites.
            # We fetched with India WIDs so it's definitely India — normalise
            # to "India" so is_india_job() in matcher.py returns True.
            if "india" not in loc_text.lower():
                loc_text = "India"
            _cache.append({
                "id": job_id,
                "title": j.get("title", "").strip(),
                "location": loc_text,
                "posting_date": "",
                "application_url": f"{_JOB_BASE}{ext_path}",
            })

        offset += _PAGE_SIZE
        if offset >= total:
            break
        time.sleep(0.2)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = _PAGE_SIZE,
    start: int = 0,
    timeout: int = 20,
) -> list[dict]:
    global _cache_filled
    if not _cache_filled:
        _cache_filled = True
        _fill_cache(timeout=timeout)
    return _cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    ext_path = application_url.replace(_JOB_BASE, "")
    detail_url = f"{_DETAIL_BASE}{ext_path}"

    for attempt in range(3):
        try:
            r = requests.get(detail_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                raise RateLimitError(f"429 on detail for {ext_path}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except Exception as exc:
            if attempt == 2:
                _desc_cache[application_url] = ("", "")
                return "", ""
            time.sleep(2 ** attempt)

    data = r.json()
    jpi = data.get("jobPostingInfo", {})
    description = _strip_html(jpi.get("jobDescription", "") or "")
    posting_date = (jpi.get("startDate") or "")[:10]

    _desc_cache[application_url] = (description, posting_date)
    return description, posting_date
