"""Fetches ShareChat (Mohalla Tech Pvt Ltd) job listings via the MyNextHire
jobboard API — the same ATS this repo already integrates for Swiggy.

ATS discovery (confirmed live 2026-08-31): sharechat.com/careers is a
server-rendered marketing shell (webpack bundle, Sanity CMS content for the
"Life at ShareChat" sections) whose "Openings" panel is filled client-side
after mount by a same-origin BFF call:

    GET https://sharechat.com/api/careersList?limit=100
    -> {"data": {"careersList": [{"title": "<dept>", "data": [...]}, ...],
                 "offsetToken": null, "count": 7, "hasNext": false}}

That BFF's own `jobDescription` field is always null (list-only), so the
real source of truth was traced one hop further: the compiled `careers`
bundle (`careers.<hash>.lazy.js`) builds job-detail links against
`https://sharechat.mynexthire.com/employer/jobs?src=careers&p={base64(...)}`
— confirming ShareChat's careers site is itself a MyNextHire tenant
(`sharechat.mynexthire.com`), not a vendor from the PLAYBOOK's Common ATS
table. Replicating Swiggy's exact reqlist contract against this tenant works
directly, with full descriptions inline and no session/CSRF needed:

    POST https://sharechat.mynexthire.com/employer/careers/reqlist/get
    Body: {"source": "careers", "code": "", "filterByBuId": -1}
    -> {"reqDetailsBOList": [ {...one job...}, ... ], "requesterTitle": "",
        "hrXmlModel": null}

Verified live: this returns the identical 7-job pool as the sharechat.com
BFF (cross-checked `reqId`/`statusId==3` sets match exactly), confirming
both are backed by the same underlying MyNextHire tenant data and the BFF
adds no additional filtering worth chasing. The direct MyNextHire endpoint
is used here because it also carries the full plain-text JD inline
(`jdDisplay`), avoiding a second per-job HTTP call — same "cache-once" idiom
as Swiggy/Groww/Razorpay/CRED/Meesho/Paytm/Lenskart/Zerodha in this repo.

**Current job pool is genuinely tiny and non-technical** (verified live,
not a fetcher bug): all 7 open requisitions as of 2026-08-31 are in
"Content & Operations" (Promo Editor, AI Animator, Video Editor, Content
Moderation Intern) or "Enablement" (Associate - Finance) — zero Software
Engineer/Developer titles, so `title_family` will legitimately reject
every current posting. This mirrors the ING (0/734 India) and eClerx (0
engineering titles) situations already documented in the PLAYBOOK's
"Company Coverage Audit" — a real current fact about ShareChat's hiring
(consistent with widely reported 2023-24 engineering-team layoffs), not a
broken integration. The fetcher is still built generically so it picks up
real Software Engineer / AI Engineer postings the moment ShareChat reopens
that pipeline; no code change will be needed.

Field mapping per job object: `reqId` (int) -> str id; `reqTitle` -> title;
`location` -> location (see quirk below); `approvedOn` (e.g.
"2026-08-25T12:01:54.470+0000") -> first 10 chars for YYYY-MM-DD;
`jdDisplay` -> full plain-text job description, already inline in the
reqlist response.

Location quirk: every observed `location` value is either the bare word
"India" or a bare city name with no "India"/state suffix ("Bangalore") —
matcher.py's is_india_job() would silently drop the city-only ones
unmodified. All 7 live postings are India-only (ShareChat's own JD
boilerplate self-describes as "India's largest homegrown social media
company" with no overseas office mentioned anywhere), the same
confirmed-India-only situation as Swiggy/First American — so ", India" is
appended unconditionally whenever "india" isn't already present in the
location text, rather than building a city-name whitelist.

Application URL quirk: the literal string built by ShareChat's own compiled
JS (`https://sharechat.mynexthire.com/employer/jobs?src=careers&p=...`,
missing a "/careers" path segment) renders an empty MyNextHire shell
("Powered By: MyNextHire.com") when navigated to directly with Playwright —
apparently only works as an in-app client-side transition, not a fresh
navigation. Adding the "/careers" segment that Swiggy's tenant uses
(`https://sharechat.mynexthire.com/employer/jobs/careers?src=careers&p=...`)
was verified live via headless Firefox to render the correct job's title,
ID, location, full JD text and a working "Apply" button on a fresh
navigation — same base64 blob shape as swiggy_fetcher.py
(`{"pageType": "jd", "cvSource": "careers", "reqId": ..., "requester":
{"id": "", "code": "", "name": ""}, "page": "careers", "bufilter": -1,
"customFields": {}}`); dropping any of those placeholder keys breaks
rendering there too.
"""
from __future__ import annotations

import base64
import json
import time

import requests

_REQLIST_URL = "https://sharechat.mynexthire.com/employer/careers/reqlist/get"
_JD_BASE = "https://sharechat.mynexthire.com/employer/jobs/careers"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://sharechat.com",
    "Referer": "https://sharechat.com/careers",
}

_REQLIST_PAYLOAD = {"source": "careers", "code": "", "filterByBuId": -1}

# Module-level cache: the full board is fetched once per process and reused
# for every keyword/page call, same idiom as swiggy_fetcher.py/
# razorpay_fetcher.py/cred_fetcher.py/lenskart_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from MyNextHire."""


def _normalize_location(raw_loc: str) -> str:
    """Append ", India" unless already present — see module docstring for
    why ShareChat's board is treated as unconditionally India (verified
    India-only, no counter-examples in the live 7-job pool)."""
    loc = (raw_loc or "").strip()
    if loc and "india" not in loc.lower():
        return f"{loc}, India"
    return loc


def _clean_text(raw: str) -> str:
    text = (raw or "").replace("\xa0", " ")
    return " ".join(text.split())


def _build_application_url(req_id: int) -> str:
    """Build a direct MyNextHire job-detail link for req_id.

    Verified live (headless Firefox) to render the correct job's title, ID,
    location, and full JD with a working Apply button — see module
    docstring for the "/careers" path-segment quirk.
    """
    blob = {
        "pageType": "jd",
        "cvSource": "careers",
        "reqId": req_id,
        "requester": {"id": "", "code": "", "name": ""},
        "page": "careers",
        "bufilter": -1,
        "customFields": {},
    }
    encoded = base64.b64encode(json.dumps(blob, separators=(",", ":")).encode()).decode()
    return f"{_JD_BASE}?src=careers&p={encoded}"


def _reqid_from_url(application_url: str) -> str:
    """Recover the reqId embedded in a URL built by _build_application_url."""
    try:
        b64 = application_url.split("p=", 1)[1].split("&", 1)[0]
        padded = b64 + "=" * (-len(b64) % 4)
        blob = json.loads(base64.b64decode(padded).decode())
        return str(blob.get("reqId", ""))
    except Exception:
        return ""


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire ShareChat MyNextHire req pool once and cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(
                _REQLIST_URL,
                headers=_HEADERS,
                json=_REQLIST_PAYLOAD,
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("ShareChat: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ShareChat cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"ShareChat cache fill: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"ShareChat cache fill: invalid JSON — {exc}") from exc

    raw_jobs = payload.get("reqDetailsBOList") or []
    collected: list[dict] = []
    for j in raw_jobs:
        req_id = j.get("reqId")
        if req_id is None:
            continue
        job_id = str(req_id)

        title = (j.get("reqTitle") or "").strip()
        if not title:
            continue

        loc = _normalize_location(j.get("location") or "")

        approved_on = j.get("approvedOn") or ""
        posting_date = approved_on[:10] if approved_on else ""

        app_url = _build_application_url(req_id)

        _desc_cache[job_id] = _clean_text(j.get("jdDisplay") or "")

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache[:] = collected
    print(f"[ShareChat] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of ShareChat jobs from the cached full req pool.

    keyword/location are accepted for interface compatibility but ignored:
    MyNextHire's own frontend fetches the full unfiltered pool and does all
    keyword/location/category filtering client-side in the browser, so there
    is no reliable server-side keyword/location contract to depend on. All
    jobs are returned — India scoping is left to matcher.py's
    is_india_job() / config exclude_locations, aside from the location
    normalisation documented in the module docstring.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single ShareChat job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    the reqlist response already includes each job's full plain-text
    description (jdDisplay), so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = _reqid_from_url(application_url)
    description = _desc_cache.get(job_id, "")

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
