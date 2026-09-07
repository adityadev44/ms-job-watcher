"""JSW Group job fetcher — TurboHire ATS via a first-party bearer-token API.

ATS discovery (live, 2026-09-07): jsw.in/careers/ links to
``https://jswgroup.turbohire.co/careerpage/9b510aa7-a9f2-46a7-aeb7-8853d81bcf10``
-- the same **TurboHire** ATS already onboarded for Flipkart
(``flipkart_fetcher.py``), a different tenant ("jswgroup") on the exact same
shared backend host (``thapi.azurewebsites.net``). The whole auth/API flow
is identical to Flipkart's:

  1. `GET /api/token/noauth` — anonymous bearer JWT, no credentials.
  2. `POST /api/careerpagev2/filteredjobs?orgId={ORG_ID}&pageType={N}` with
     `Authorization: Bearer <token>` and a JSON filter body — returns
     `{"Total": N, "Result": [...]}`, unpaginated (Result length always
     equals Total).
  3. `GET /api/referraljobs?tkn={JobIdObfuscated}&fieldVisibility=CareerPage`
     — full, untruncated job detail (`JobDescriptionV2`). The list
     response's own `JobDescV2` is silently truncated at 500 characters
     (confirmed live, same as Flipkart) -- not reused here.

**`pageType` is tenant-specific, not a shared convention — re-verified live
rather than assumed from Flipkart's precedent.** Flipkart's tenant uses
`pageType=1` for its big company-wide pool; probing JSW's tenant directly
gave (Total for pageType 0/1/2/3) = (58, 14, 28, 58) — a completely
different shape. Clicking JSW's own "View All Jobs" button in a real
Playwright session and capturing the resulting network request confirmed
the real, user-facing board is `pageType=0` (`dashboardv2?type=0` is the
URL the click lands on, and its `filteredjobs` call uses `pageType=0`) --
this also matches the department-level counts shown on the careerpage
itself (Engineering 27, IT & Digital 1+7, Sales & Marketing 4+5, etc.,
summing to 58). `pageType=0` is used here; the other three page types were
not investigated further since they are not what a real visitor sees.

Server-side filtering — tested live against the real API: `Keyword`
genuinely narrows (confirmed: a nonsense token returns `Total: 0`), but
every one of this repo's standard keywords ALSO currently returns `Total:
0` against JSW's live 58-job pool -- a genuine "zero is a fact" result
today (the board is dominated by automotive design/engineering roles for
JSW's EV business, JSW Motors Limited, at its Chhatrapati Sambhajinagar/
Aurangabad plant -- BIW/CAE/Powertrain/Trims & Lighting Design Engineer
titles -- plus IT/Cybersecurity leadership roles, none of which literally
match the configured `.NET`/AI-ML/Python title_family phrases right now).
Given the whole pool is only 58 postings, this fetcher caches it once per
process (same "cache-once, let matcher.py do the real narrowing" reasoning
as Flipkart/RIL/Vedanta elsewhere in this repo) rather than re-querying a
58-job board once per keyword in the default 10-keyword list. The pipeline
is mechanically correct and will surface a real match automatically the
moment JSW posts a matching title.

Location: every posting's `Location` JSON blob already carries a plain
"..., India" (or "..., Maharashtra, India, (Plant)") address string with no
overseas postings observed in the live 58-job pool -- used as-is (with a
defensive ", India" append if a future posting's address ever omits it).

Other quirks (identical to Flipkart's tenant, same shared TurboHire
frontend):
- `JobIdObfuscated` (used in both the apply URL and the detail endpoint
  lookup) arrives already percent-encoded (a literal `%2F` where the
  underlying token has a `/`) -- spliced into URLs as-is, never passed
  through `requests`' `params=` dict (which would double-encode `%` into
  `%25` and break the lookup).
- Application URL: `https://jswgroup.turbohire.co/job/publicjobs/{obfuscated}`
  (same convention as Flipkart's tenant).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_ORG_ID = "9b510aa7-a9f2-46a7-aeb7-8853d81bcf10"
_API_BASE = "https://thapi.azurewebsites.net/api"
_TOKEN_URL = f"{_API_BASE}/token/noauth"
_FILTEREDJOBS_URL = f"{_API_BASE}/careerpagev2/filteredjobs"
_DETAIL_URL = f"{_API_BASE}/referraljobs"
_CAREER_BASE = "https://jswgroup.turbohire.co"
_PAGE_TYPE = 0  # the real "View All Jobs" pool -- confirmed live, see module docstring

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

# Module-level cache: the filteredjobs pool is fetched once and reused for
# every keyword/location call in this process (Honeywell/Persistent lesson:
# _cache_filled is set to True *before* the fetch attempt so a transient
# failure doesn't retry-storm on every subsequent fetch_jobs() call).
# _cache_error additionally persists a fill failure across calls so a
# second fetch_jobs() in the same process re-raises instead of silently
# returning an empty (and wrong) "0 jobs" success.
_job_cache: list[dict] = []
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None

# Bearer token cache — the anonymous "noauth" token is good for
# `expires_in` seconds (observed: 3600) and is not job-specific, so it is
# reused across every request in this process rather than re-fetched per call.
_bearer_token: str | None = None
_token_expiry: float = 0.0


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw_iso: str) -> str:
    """'2026-09-04T06:30:04.353873Z' -> '2026-09-04'."""
    return (raw_iso or "")[:10]


def _job_id_from_url(application_url: str) -> str:
    """'.../job/publicjobs/{obfuscated}' -> '{obfuscated}' (kept pre-encoded)."""
    return (application_url or "").rsplit("/job/publicjobs/", 1)[-1]


def _location_from_job(raw_location: str) -> str:
    try:
        entries = json.loads(raw_location or "[]")
    except ValueError:
        entries = []
    addrs = [e.get("Address", "").strip() for e in entries if e.get("Address")]
    if not addrs:
        return "India"
    loc_str = "; ".join(addrs)
    if "india" not in loc_str.lower():
        loc_str = f"{loc_str}, India"
    return loc_str


def _get_token(timeout: int = 20) -> str:
    """Return a cached anonymous bearer token, refreshing if missing/expired."""
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
                raise RateLimitError("JSW token: 429 rate-limited")
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
            raise RateLimitError(f"JSW token fetch failed: {exc}") from exc

    raise RateLimitError(f"JSW token fetch: no response -- {last_exc}")


def _auth_headers(timeout: int = 20) -> dict:
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {_get_token(timeout=timeout)}"
    return headers


def _fetch_filteredjobs(timeout: int) -> dict:
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
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("JSW filteredjobs: 429 rate-limited")
            if r.status_code == 401:
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("JSW filteredjobs: 401 after retry")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"JSW filteredjobs fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"JSW filteredjobs: no response -- {last_exc}")

    try:
        return r.json()
    except ValueError as exc:
        raise RateLimitError(f"JSW filteredjobs: invalid JSON -- {exc}") from exc


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full job pool once and cache it.

    _cache_filled is set before the request attempt so a failure doesn't
    trigger a retry storm on every fetch_jobs()/fetch_job_description() call
    made during the same process run. A failure is also remembered in
    _cache_error and re-raised on every subsequent call so this never
    silently degrades into a false "0 jobs" success.
    """
    global _cache_filled, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    try:
        payload = _fetch_filteredjobs(timeout)
    except RateLimitError as exc:
        _cache_error = exc
        raise

    results = payload.get("Result") or []
    seen_ids: set[str] = set()
    collected: list[dict] = []
    for j in results:
        job_id = str(j.get("JobId") or "").strip()
        title = (j.get("JobTitle") or "").strip()
        obfuscated = j.get("JobIdObfuscated") or ""
        if not (job_id and title and obfuscated) or job_id in seen_ids:
            continue

        seen_ids.add(job_id)
        collected.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(j.get("Location") or ""),
            "posting_date": _parse_date(j.get("PublishedDate") or j.get("UpdatedDate") or ""),
            "application_url": f"{_CAREER_BASE}/job/publicjobs/{obfuscated}",
        })

    _job_cache[:] = collected
    print(f"[JSW] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of JSW Group postings from the cached full pool.

    keyword/location are accepted for interface compatibility but ignored —
    TurboHire's `Keyword` filter is genuinely respected server-side
    (verified: nonsense tokens return Total=0), but the whole (small,
    ~58-job) pool is cached once and the shared matcher does the real
    title/skill/India filtering against the cached slice.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single JSW job.

    Calls the /api/referraljobs detail endpoint for the full, untruncated
    description — the search response's own JobDescV2 field is silently
    truncated at ~500 characters (confirmed live), so it is not reused here.
    """
    obfuscated = _job_id_from_url(application_url)
    if not obfuscated:
        return "", ""

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            headers = _auth_headers(timeout=timeout)
            # Splice the token in directly — it already arrives pre-encoded
            # (literal "%2F" for "/"); routing it through requests' params=
            # would double-encode the "%" and break the lookup.
            url = f"{_DETAIL_URL}?tkn={obfuscated}&fieldVisibility=CareerPage"
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("JSW description: 429 rate-limited")
            if r.status_code == 401:
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("JSW description: 401 after retry")
            if r.status_code in (400, 404):
                # Closed/stale posting — not a transient failure, so don't
                # retry-storm on it.
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
            raise RateLimitError(f"JSW description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"JSW description fetch: no response -- {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"JSW description: invalid JSON -- {exc}") from exc

    desc_html = data.get("JobDescriptionV2") or data.get("JobDescription") or ""
    description = _strip_html(desc_html)
    posting_date = _parse_date(data.get("PublishedDate") or data.get("UpdatedDate") or "")
    return description, posting_date
