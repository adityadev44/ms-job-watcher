"""
Arista Networks job fetcher -- SmartRecruiters public REST API.

careers.smartrecruiters.com/AristaNetworks confirmed live 2026-09-14 --
same platform family already in this repo via Nagarro/NECSWS/PhonePe/
Eurofins. Company identifier confirmed via `api.smartrecruiters.com/v1/
companies/AristaNetworks/postings`, matching the public
`jobs.smartrecruiters.com`/`careers.smartrecruiters.com/AristaNetworks/...`
apply links.

Arista is a smaller US networking vendor than Cisco/Ericsson/Nokia, but its
Bengaluru/Chennai/Pune India engineering presence turned out real and
substantial: `country=in` is a reliable server-side filter returning 26 of
the global pool, and unlike many GCC/telecom-vendor onboardings in this
repo, the majority of those 26 titles are genuine IC software-engineering
roles -- "Software Engineer, Network Systems", "Software Engineer
(Platform/EOS/Networking)", "Software Engineer- SONiC", "Diagnostics
Software Engineer", "Software Developer(SRE) - CloudVision as a Service
(CVaaS)", "Software Test Engineer (Wi-Fi/Cloud)", "Software Engineer
(Routing Protocols)" -- not just sales/hardware roles. No `require_tech_in_
description` needed: titles alone give a strong signal, and Layer 3's skill
gate handles precision from there (this is a systems-software/embedded/SRE
shop -- expect the AI/ML/Python track and Golang/Python skills to fire more
than .NET/C#, which is rare at a pure networking-hardware vendor).

Verified no Tamil Nadu location leak (unlike Eurofins' `region: "TN"`
quirk): Chennai postings here already carry the literal city name "Chennai"
in `location.city`, which config's default `exclude_locations` catches
directly -- no region-code append needed.

Job descriptions are NOT inline in the search response -- fetched from the
same SmartRecruiters detail endpoint (`GET /v1/companies/AristaNetworks/
postings/{id}`), same `jobAd.sections` shape as Eurofins/Nagarro
(`jobDescription` + `qualifications` + `additionalInformation` concatenated;
`companyDescription` is generic boilerplate, excluded).
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BASE_URL = "https://api.smartrecruiters.com/v1/companies/AristaNetworks/postings"
_PUBLIC_BASE = "https://jobs.smartrecruiters.com/AristaNetworks"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.arista.com/en/careers/india/current-openings-ind",
}

_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from SmartRecruiters."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """'2026-09-11T20:34:55.930Z' -> '2026-09-11'."""
    return raw[:10] if raw else ""


def _build_location(loc: dict) -> str:
    city = (loc.get("city") or "").strip()
    if not city or city.lower() == "india":
        return "India"
    return f"{city}, India"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return a page of Arista's India job postings.

    India is scoped server-side via `country=in` (verified reliable -- 26 of
    the global pool, every sample a real India city). `keyword` is passed
    through but not confirmed to narrow server-side on this tenant (same
    "harmless either way, dedup handles it" reasoning as other SmartRecruiters
    fetchers here); `location` is not sent.
    """
    params = {
        "q": keyword,
        "country": "in",
        "limit": min(num, 100),
        "offset": start,
    }

    r = None
    for attempt in range(3):
        try:
            r = requests.get(_BASE_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Arista search: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Arista search failed: {exc}") from exc

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Arista search returned non-JSON body: {exc}") from exc

    jobs: list[dict] = []
    for j in payload.get("content", []):
        job_id = str(j.get("id") or "").strip()
        title = (j.get("name") or "").strip()
        if not job_id or not title:
            continue

        loc = j.get("location", {}) or {}
        country = (loc.get("country") or "").strip()
        if country and country.lower() != "in":
            continue

        location_str = _build_location(loc)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            "posting_date": _parse_date(j.get("releasedDate", "")),
            "application_url": f"{_PUBLIC_BASE}/{job_id}",
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch job description + posting date via the SmartRecruiters detail API.

    Concatenates `jobDescription`, `qualifications`, and
    `additionalInformation` sections; `companyDescription` is generic
    boilerplate, excluded.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    job_id = application_url.rstrip("/").split(f"{_PUBLIC_BASE}/")[-1].split("-")[0]

    r = None
    for attempt in range(3):
        try:
            r = requests.get(f"{_BASE_URL}/{job_id}", headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Arista description: 429 rate-limited for {job_id}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            raise RateLimitError(f"Arista description fetch failed: {exc}") from exc

    try:
        detail = r.json()
    except ValueError:
        return "", ""

    sections = detail.get("jobAd", {}).get("sections", {}) or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        txt = (sections.get(key) or {}).get("text", "")
        if txt:
            parts.append(_strip_html(txt))
    description = " ".join(parts)

    posting_date = _parse_date(detail.get("releasedDate", ""))

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
