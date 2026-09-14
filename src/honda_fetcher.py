"""Fetches Honda job listings — Honda Motorcycle & Scooter India (HMSI) via
the TurboHire ATS, same vendor/backend already used by `flipkart_fetcher.py`.

ATS discovery (2026-09-13/14): investigated three separate Honda India
entities before finding a working automatable board:

- **Honda Cars India** (`hondacarindia.com/careers`) — a pure branding/
  culture page with zero job-search functionality, no listings, no ATS
  integration of any kind. Genuine dead end (like Mu Sigma elsewhere in this
  repo), not attempted here.
- **Honda R&D India (HRID, Manesar)** (`honda.hrid.in/careers/`) — a direct
  HR landing page, not an ATS: exactly one job ("Creative Designer") is
  hardcoded as a static link, with all other applications directed to a
  plain `mailto:` address. No search API, no automatable board.
- **Honda Motorcycle & Scooter India (HMSI)**, the two-wheeler
  manufacturing arm HQ'd in Gurugram, links its careers page
  (`honda2wheelersindia.com/about/careers`) to a real, working
  **TurboHire** career page: `hmsi.turbohire.co/careerpage/
  ac210007-77a2-4e11-91b9-79697a126680`. This is the exact same ATS/backend
  family as `flipkart_fetcher.py` (`thapi.azurewebsites.net`), just a
  different TurboHire org id and tenant subdomain.

Endpoint sequence (identical shape to Flipkart's, see that module's
docstring for the full API contract):

  1. `GET https://thapi.azurewebsites.net/api/token/noauth` — anonymous
     bearer JWT. **Difference from Flipkart**: this endpoint 403s
     ("Invalid request.") without `Origin`/`Referer` headers naming the
     `hmsi.turbohire.co` tenant explicitly — Flipkart's tenant did not
     require this, so it is not a universal TurboHire requirement, just a
     per-tenant WAF/CORS check worth expecting at any future TurboHire
     integration.
  2. `POST /api/careerpagev2/filteredjobs?orgId={ORG_ID}&pageType={N}` —
     same JSON filter body as Flipkart. Tested pageType 0/1/2/3 directly:
     **all four return `Total: 0`** on this tenant as of 2026-09-14 — live-
     confirmed genuine via a real headless-Chromium session rendering the
     public career page itself, which shows "Sorry, Currently there are no
     vacancies." This is a real, working, feasible pipeline with a
     currently-empty board, not a fetcher bug (same class of finding as
     Nykaa's empty Darwinbox tenant elsewhere in this repo) — parsing logic
     is identical to and validated against Flipkart's populated TurboHire
     org, which returns real jobs through the exact same code path.
  3. `GET /api/referraljobs?tkn={JobIdObfuscated}&fieldVisibility=CareerPage`
     — full per-job description, same as Flipkart.

HMSI's own `/api/organizations/{orgId}/verifieddomains` confirms this is a
genuine active org tenant (`@honda.hmsi.in`), not an abandoned/demo
TurboHire account.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_ORG_ID = "ac210007-77a2-4e11-91b9-79697a126680"
_API_BASE = "https://thapi.azurewebsites.net/api"
_TOKEN_URL = f"{_API_BASE}/token/noauth"
_FILTEREDJOBS_URL = f"{_API_BASE}/careerpagev2/filteredjobs"
_DETAIL_URL = f"{_API_BASE}/referraljobs"
_CAREER_BASE = "https://hmsi.turbohire.co"
_PAGE_TYPE = 1  # full company-wide "All Jobs" pool — see flipkart_fetcher.py

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": _CAREER_BASE,
    "Referer": f"{_CAREER_BASE}/careerpage/{_ORG_ID}",
}

_FILTER_BODY = {
    "SortByV2": {"Key": "PostedDate", "Order": 2},
    "BunitIds": {"Value": None, "FilterType": 0},
    "Experience": {"Value": None, "FilterType": 0},
    "JobTypes": {"Value": None, "FilterType": 0},
    "JobTypeV2": {"Value": None, "FilterType": 0},
    "Locations": {"Value": None, "FilterType": 0},
    "CreatedDate": {"Value": None, "FilterType": 0},
    "Compensation": {"Value": None, "FilterType": 0},
    "Skills": {"Value": None, "FilterType": 0},
    "Keyword": "",
    "ClientIds": {"Value": None, "FilterType": 0},
    "Department": "",
    "CustomFields": {},
}

_india_cache: list[dict] = []
_cache_filled: bool = False

_bearer_token: str | None = None
_token_expiry: float = 0.0


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw_iso: str) -> str:
    return (raw_iso or "")[:10]


def _job_id_from_url(application_url: str) -> str:
    return (application_url or "").rsplit("/job/publicjobs/", 1)[-1]


def _get_token(timeout: int = 20) -> str:
    global _bearer_token, _token_expiry
    if _bearer_token and time.time() < _token_expiry:
        return _bearer_token

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_TOKEN_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Honda token: 429 rate-limited")
            r.raise_for_status()
            data = r.json()
            _bearer_token = data["access_token"]
            _token_expiry = time.time() + max(int(data.get("expires_in", 3600)) - 60, 60)
            return _bearer_token
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError, KeyError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Honda token fetch failed: {exc}") from exc

    raise RateLimitError(f"Honda token fetch: no response — {last_exc}")


def _auth_headers(timeout: int = 20) -> dict:
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {_get_token(timeout=timeout)}"
    return headers


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    request_timeout = max(timeout, 45)

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            headers = _auth_headers(timeout=timeout)
            r = requests.post(
                _FILTEREDJOBS_URL,
                params={"orgId": _ORG_ID, "pageType": _PAGE_TYPE},
                headers=headers,
                json=_FILTER_BODY,
                timeout=request_timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Honda filteredjobs: 429 rate-limited")
            if r.status_code == 401:
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Honda filteredjobs: 401 after retry")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Honda filteredjobs fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Honda filteredjobs: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Honda filteredjobs: invalid JSON — {exc}") from exc

    results = payload.get("Result") or []
    seen_ids: set[str] = set()
    collected: list[dict] = []
    for j in results:
        job_id = str(j.get("JobId") or "").strip()
        title = (j.get("JobTitle") or "").strip()
        obfuscated = j.get("JobIdObfuscated") or ""
        if not (job_id and title and obfuscated) or job_id in seen_ids:
            continue

        try:
            loc_entries = json.loads(j.get("Location") or "[]")
        except ValueError:
            loc_entries = []
        addrs = [e.get("Address", "").strip() for e in loc_entries if e.get("Address")]
        if not addrs:
            continue
        loc_str = "; ".join(addrs)
        if "india" not in loc_str.lower():
            loc_str = f"{loc_str}, India"

        seen_ids.add(job_id)
        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_str,
            "posting_date": _parse_date(j.get("PublishedDate") or j.get("UpdatedDate") or ""),
            "application_url": f"{_CAREER_BASE}/job/publicjobs/{obfuscated}",
        })

    _india_cache[:] = collected
    print(f"[Honda/HMSI] Cache filled: {len(collected)} India jobs (of {len(results)} total company-wide postings)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Honda (HMSI) India postings from the cached pool.

    keyword/location are accepted for interface compatibility but ignored —
    same reasoning as flipkart_fetcher.py's identical TurboHire shape.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    obfuscated = _job_id_from_url(application_url)
    if not obfuscated:
        return "", ""

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            headers = _auth_headers(timeout=timeout)
            url = f"{_DETAIL_URL}?tkn={obfuscated}&fieldVisibility=CareerPage"
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Honda description: 429 rate-limited")
            if r.status_code == 401:
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Honda description: 401 after retry")
            if r.status_code in (400, 404):
                return "", ""
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Honda description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Honda description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Honda description: invalid JSON — {exc}") from exc

    desc_html = data.get("JobDescriptionV2") or data.get("JobDescription") or ""
    description = _strip_html(desc_html)
    posting_date = _parse_date(data.get("PublishedDate") or data.get("UpdatedDate") or "")
    return description, posting_date
