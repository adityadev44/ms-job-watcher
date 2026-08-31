"""Flipkart job fetcher — TurboHire ATS via a first-party bearer-token API.

ATS discovery (2026-08-31): flipkart.com/about/careers itself now 404s (even
inside a real headless-Chromium session — the reCAPTCHA wall a plain
`requests` GET sees is Akamai/PerimeterX bot-checking a page that no longer
exists). The real, currently-live careers destination is a separate marketing
site, flipkartcareers.com, whose one "Explore Opportunities" link points to:

    https://flipkart.turbohire.co/careerpage/4d757ba0-3d57-448a-b82c-238ed87ac90f

confirming **TurboHire** (an Indian ATS, view-source shows a `turbohire.co`
asset host). This is a React SPA that renders a completely blank
`<div id="root">` under headless **Firefox** — a page-crashing
`p.appVersion.match(...)` call (a browser-sniffing helper the bundle ships)
throws on Firefox's `navigator.appVersion` shape, which kills the whole React
tree before anything mounts. Switching to headless **Chromium** (also
pre-installed in the /tmp/mjw_venv Playwright cache) renders normally — the
opposite lesson from Honeywell/IBM/Tech Mahindra elsewhere in this repo,
where Chromium is the one that gets blocked. Once rendered, network capture
showed the real backing API host: `thapi.azurewebsites.net`.

No browser is needed at runtime, though — the whole flow is a plain,
unauthenticated REST sequence:

  1. `GET /api/token/noauth` — returns a bearer JWT (`expires_in: 3600`) with
     zero credentials. The endpoint's own name says what it is: this is the
     intentional anonymous-visitor auth path TurboHire's own frontend uses
     for public candidates, not a workaround.
  2. `POST /api/careerpagev2/filteredjobs?orgId={ORG_ID}&pageType={N}` with
     `Authorization: Bearer <token>` and a JSON filter body — returns
     `{"Total": N, "Result": [...]}`, unpaginated (Result length always
     equals Total, confirmed up to ~6700 rows in one response).
  3. `GET /api/referraljobs?tkn={JobIdObfuscated}&fieldVisibility=CareerPage`
     (same bearer token) — full, untruncated job detail (`JobDescriptionV2`).
     The list response's own `JobDescV2` field is silently truncated at
     ~500 characters, so this detail call is not optional for accuracy.

**`pageType` quirk — three different job pools behind one org, and the UI is
an unreliable guide to which is which.** Flipkart's TurboHire org publishes
jobs to multiple simultaneous boards (seen in a job's own
`PublishedJobBoards` field: `INTERNALJOBSPAGE`, `LINKEDINJOBS`, `NAUKRI`,
`FREE_JOBBOARDS`, `CAREERPAGE`, `REFERRALJOBSPAGE`). Querying `pageType`
0/1/2/3 directly against the API gave stable, repeatable (Total, count)s of
(6, 6691, 60, 6) respectively across multiple separate calls — but loading
the equivalent `dashboardv2?type=N` URL in a real anonymous headless-Chromium
session was flaky about which pool it displayed (a client-side SPA race, not
a permissions wall — a *fresh, cookie-less* browser context sometimes
rendered the full ~6690-job pool at `type=1` with zero login, and other times
rendered 0, confirming the instability lives in the frontend's own async
state resolution, not the backend). This fetcher calls the REST API directly
(`pageType=1`, ~6690 jobs company-wide as of 2026-08-31) rather than trusting
the SPA's own routing, since the direct API calls were reproducibly stable
across half a dozen separate test runs while the in-browser UI was not.
`pageType=0` is the tiny, hand-curated set (6 jobs, 2026-08-31) actually
linked from flipkartcareers.com's marketing page — real, but 100%
warehouse/ops roles (Branch Manager, Warehouse Inventory Manager, FM & LM
Ops) with zero engineering titles; `pageType=1` is Flipkart's full
company-wide open-requisition pool (same one a fresh anonymous browser
session was independently observed rendering under the "All Jobs" board),
which is where genuine `SDE`/`Software Development Engineer`/`Data Engineer`
titles actually live (GCP, Shopsy Engg, FSE-Engg, Ads, Refurb, Payments
departments, Bengaluru/Hyderabad). Using `pageType=0` alone would make this
pipeline structurally incapable of ever alerting on a Flipkart engineering
role, since the marketing career page never lists any.

Other quirks:
- `JobIdObfuscated` (the token used in both the apply URL and the detail
  endpoint) comes back from the API **already percent-encoded** (its raw
  string value contains literal `%2F` characters where the underlying token
  has a `/`). Splice it into URLs as-is — passing it through
  `requests`' `params=` dict double-encodes the `%` into `%25` and breaks
  both the apply link and the detail lookup. Confirmed by comparing this
  fetcher's constructed URL against the real URL a Playwright click-through
  landed on: `https://flipkart.turbohire.co/job/publicjobs/{JobIdObfuscated}`.
- The `filteredjobs` POST for the full ~6690-job pool is large (~11 MB) and
  routinely takes 5–22 seconds — comfortably over the default `timeout=20`
  passed in by `matcher.py`. The cache-fill request therefore always uses
  `max(timeout, 45)` internally regardless of the caller's value.
- `Keyword` in the POST body IS genuinely respected server-side (confirmed:
  `"software engineer"` narrows `Total` correctly) — but the full pool is
  cached once anyway (same "Honeywell lesson" `_cache_filled` pattern as
  Atlassian/Groww/Persistent/MSCI) since matcher.py issues many keyword
  passes and re-fetching an 11 MB payload per keyword would be wasteful.
- Live-verified 2026-08-31: a real "SDE-2 Hiring for Recommerce" posting
  (Refurb dept, Bengaluru) lists "Java, Python, Go, C++, or C#" as acceptable
  languages in its body — a genuine (if incidental) `.NET / C#` primary-skill
  hit via the bare "C#" mention, proving this data source can and does
  produce real matches through the existing shared matcher, not just
  plumbing that returns zero.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_ORG_ID = "4d757ba0-3d57-448a-b82c-238ed87ac90f"
_API_BASE = "https://thapi.azurewebsites.net/api"
_TOKEN_URL = f"{_API_BASE}/token/noauth"
_FILTEREDJOBS_URL = f"{_API_BASE}/careerpagev2/filteredjobs"
_DETAIL_URL = f"{_API_BASE}/referraljobs"
_CAREER_BASE = "https://flipkart.turbohire.co"
_PAGE_TYPE = 1  # full company-wide "All Jobs" pool — see module docstring

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
_india_cache: list[dict] = []
_cache_filled: bool = False

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
    """'2026-08-17T11:38:33.0059552Z' -> '2026-08-17'."""
    return (raw_iso or "")[:10]


def _job_id_from_url(application_url: str) -> str:
    """'.../job/publicjobs/{obfuscated}' -> '{obfuscated}' (kept pre-encoded)."""
    return (application_url or "").rsplit("/job/publicjobs/", 1)[-1]


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
                raise RateLimitError("Flipkart token: 429 rate-limited")
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
            raise RateLimitError(f"Flipkart token fetch failed: {exc}") from exc

    raise RateLimitError(f"Flipkart token fetch: no response — {last_exc}")


def _auth_headers(timeout: int = 20) -> dict:
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {_get_token(timeout=timeout)}"
    return headers


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full company-wide job pool once and cache India postings.

    _cache_filled is set before the request attempt so a failure doesn't
    trigger a retry storm on every fetch_jobs()/fetch_job_description() call
    made during the same process run.
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    # The ~6690-job response is ~11 MB and has been observed taking 5-22s;
    # always use a generous floor here regardless of the caller's timeout.
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
                raise RateLimitError("Flipkart filteredjobs: 429 rate-limited")
            if r.status_code == 401:
                # Stale/expired token — force a refresh on the next attempt.
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Flipkart filteredjobs: 401 after retry")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Flipkart filteredjobs fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Flipkart filteredjobs: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Flipkart filteredjobs: invalid JSON — {exc}") from exc

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
    print(f"[Flipkart] Cache filled: {len(collected)} India jobs (of {len(results)} total company-wide postings)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Flipkart India postings from the cached full pool.

    keyword/location are accepted for interface compatibility but ignored —
    although TurboHire's `Keyword` filter is genuinely respected server-side
    (verified), the whole pool is cached once (~6690 jobs) so that matcher.py's
    many keyword passes don't each re-fetch an ~11 MB response. The shared
    matcher does the real title/skill filtering against the cached slice.
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Flipkart job.

    Calls the /api/referraljobs detail endpoint for the full, untruncated
    description — the search response's own JobDescV2 field is silently
    truncated at ~500 characters, so it is not reused here.
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
                raise RateLimitError("Flipkart description: 429 rate-limited")
            if r.status_code == 401:
                global _bearer_token
                _bearer_token = None
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Flipkart description: 401 after retry")
            if r.status_code in (400, 404):
                # Closed/stale posting (Infosys-style link rot) — not a
                # transient failure, so don't retry-storm on it.
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
            raise RateLimitError(f"Flipkart description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Flipkart description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Flipkart description: invalid JSON — {exc}") from exc

    desc_html = data.get("JobDescriptionV2") or data.get("JobDescription") or ""
    description = _strip_html(desc_html)
    posting_date = _parse_date(data.get("PublishedDate") or data.get("UpdatedDate") or "")
    return description, posting_date
