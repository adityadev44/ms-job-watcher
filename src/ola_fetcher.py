"""Ola job fetcher — TurboHire ATS via a first-party bearer-token API.

ATS discovery (2026-09-14): `www.olacabs.com/careers` is a server-rendered
page whose real "Explore Opportunities"-style link points at:

    https://olacareers.turbohire.co/careerpage/e0c1eb37-eb7a-4ca4-bcc5-d59ce4ce9212

confirming **TurboHire** — the same ATS already integrated for Flipkart in
this repo (`flipkart_fetcher.py`), a different org/tenant. The org's own
`publicorganizations/{orgId}` metadata resolves the account name to "ANI
Technologies Pvt Ltd" (Ola Cabs' legal entity) but the career page's own
banner copy reads "Explore Boundless Horizons: Your Career Awaits at Ola
Electric" — this single TurboHire org appears to be a shared talent-pool
page spanning both the ride-hailing (Ola Cabs / ANI Technologies) and Ola
Electric brands under Bhavish Aggarwal's group, not something split into
separate boards.

**Different API host than Flipkart's tenant.** A live Playwright network
capture of the real page load shows the frontend's `token/noauth` bearer
call goes to `api.turbohire.co` (not `thapi.azurewebsites.net`, the host
Flipkart's tenant uses) — TurboHire apparently serves different customers
from different backend hosts. Both hosts were tested directly against this
org's `orgId` and return byte-identical results (both plain, unauthenticated
REST, same request/response shape as `flipkart_fetcher.py`), so this
fetcher uses `thapi.azurewebsites.net` for consistency with the existing
Flipkart integration — confirmed interchangeable, not required.

Same 3-call flow as Flipkart:
  1. `GET /api/token/noauth` — anonymous bearer JWT, no credentials.
  2. `POST /api/careerpagev2/filteredjobs?orgId={ORG_ID}&pageType={N}` with
     `Authorization: Bearer <token>` — returns `{"Total": N, "Result": [...]}`.
  3. `GET /api/referraljobs?tkn={JobIdObfuscated}&fieldVisibility=CareerPage`
     — full job detail.

**`pageType` swept 0 through 7 — every single one returns `Total: 0`,
confirmed reproducibly across multiple separate calls and both API hosts.**
Unlike Flipkart (whose `pageType=1` "All Jobs" pool has ~6690 postings),
this Ola/Ola-Electric TurboHire org currently has a genuinely empty career
page across every board type TurboHire exposes — not a wrong `pageType`
guess or an auth/param bug. `pageType=1` is used here for consistency with
Flipkart's fetcher (the pool with the most realistic chance of containing
engineering postings once any exist).

This is the same disposition class as `zomato_fetcher.py`'s "genuinely zero
open postings, not a fetcher bug" finding: the plumbing is proven correct
against a working sibling org (Flipkart, same product, same request shape,
thousands of real jobs) — it simply has nothing to return today. New Ola
postings on this board will be picked up automatically the next time this
pipeline runs, no code change needed.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_ORG_ID = "e0c1eb37-eb7a-4ca4-bcc5-d59ce4ce9212"
_API_BASE = "https://thapi.azurewebsites.net/api"
_TOKEN_URL = f"{_API_BASE}/token/noauth"
_FILTEREDJOBS_URL = f"{_API_BASE}/careerpagev2/filteredjobs"
_DETAIL_URL = f"{_API_BASE}/referraljobs"
_CAREER_BASE = "https://olacareers.turbohire.co"
_PAGE_TYPE = 1  # see module docstring — every pageType is currently empty

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

# Module-level cache (Honeywell/Flipkart lesson: _cache_filled is set before
# the fetch attempt so a transient failure doesn't retry-storm on every
# subsequent fetch_jobs()/fetch_job_description() call in the same process).
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
                raise RateLimitError("Ola token: 429 rate-limited")
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
            raise RateLimitError(f"Ola token fetch failed: {exc}") from exc

    raise RateLimitError(f"Ola token fetch: no response — {last_exc}")


def _auth_headers(timeout: int = 20) -> dict:
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {_get_token(timeout=timeout)}"
    return headers


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

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
                timeout=max(timeout, 30),
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Ola filteredjobs: 429 rate-limited")
            if r.status_code == 401:
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Ola filteredjobs: 401 after retry")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Ola filteredjobs fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Ola filteredjobs: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Ola filteredjobs: invalid JSON — {exc}") from exc

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
    print(f"[Ola] Cache filled: {len(collected)} India jobs (of {len(results)} total company-wide postings)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Ola India postings from the cached full pool.

    keyword/location are accepted for interface compatibility but ignored —
    this org's board is currently empty for every pageType (see module
    docstring); once populated, TurboHire's own `Keyword` filter is
    genuinely respected server-side (confirmed on Flipkart's sibling org),
    but the whole pool is still cached once per process to avoid re-fetching
    per keyword.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Ola job."""
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
                raise RateLimitError("Ola description: 429 rate-limited")
            if r.status_code == 401:
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Ola description: 401 after retry")
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
            raise RateLimitError(f"Ola description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Ola description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Ola description: invalid JSON — {exc}") from exc

    desc_html = data.get("JobDescriptionV2") or data.get("JobDescription") or ""
    description = _strip_html(desc_html)
    posting_date = _parse_date(data.get("PublishedDate") or data.get("UpdatedDate") or "")
    return description, posting_date
