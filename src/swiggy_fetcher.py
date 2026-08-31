"""Fetches Swiggy job listings via the MyNextHire jobboard API.

ATS discovery (2026-08-31): careers.swiggy.com/list.html is a dead, S3-cached
leftover from a 2019-era AngularJS build (Last-Modified 2019-08-14) whose own
script.js still points at a long-dead "developer.hirexp.com" API (verified:
that host no longer resolves/responds at all) — this is NOT the live careers
site despite being the URL that search results point at. The real, currently
served site is https://careers.swiggy.com/ (no /list.html), a newer Angular
SPA (Last-Modified 2025-05-06) that embeds Swiggy's actual ATS, MyNextHire,
in an iframe via ./assets/js/careers-integration.js, which builds:
    https://{clientShortName}.mynexthire.com/employer/jobs/careers
with `clientShortName` = "swiggy" (confirmed from the compiled Angular
bundle's `environment.ts`: `employerShortName: "swiggy"`,
`api_url: 'https://swiggy.mynexthire.com/employer/careers/reqlist/get'`).

That iframe itself loads a controller script literally named `careers.js`
(https://swiggy.mynexthire.com/employer/ui/js/jobboard/careers.js) which is
the real jobboard logic. It POSTs to `careersFactory.smaclifyUrl +
"/careers/reqlist/get"` (== the api_url above) with a small fixed payload and
renders `response.reqDetailsBOList` — confirmed live:

    POST https://swiggy.mynexthire.com/employer/careers/reqlist/get
    Body: {"source": "careers", "code": "", "filterByBuId": -1}
    -> {"reqDetailsBOList": [ {...one job...}, ... ], "requesterTitle": "",
        "hrXmlModel": null}

The endpoint takes no keyword/location/pagination params and returns the
entire current pool of publicly-open requisitions in one call (79 jobs
verified live, every one `statusId == 3`) — same "cache-once" idiom as
Groww/Razorpay/CRED/Meesho/Paytm/Lenskart in this repo. The Angular app does
all keyword/location/category filtering client-side against this same
unfiltered array (see `career-service.service.ts` in the compiled bundle),
confirming server-side filtering isn't a thing to rely on here.

Field mapping per job object: `reqId` (int) -> str id; `reqTitle` -> title
(`designation` is a similar but occasionally-divergent internal leveling
field — `reqTitle` is what the live UI actually renders as the job title,
confirmed by opening real postings in Playwright); `location` -> location,
bare city name with no "India"/state suffix at all (see quirk below);
`approvedOn` (e.g. "2026-08-28T10:20:33.427+0000") -> first 10 chars for
YYYY-MM-DD; `jdDisplay` -> the full plain-text job description, already
inline in the search response — no separate per-job detail call is needed
(same as spglobal_careers/gallagher/persistent-style inline-description
fetchers). `jdDisplay` is plain text (no HTML tags observed), just needs
whitespace/non-breaking-space normalisation.

Location quirk: every observed location is a bare Indian city/town name
("Bangalore", "Trichy", "Sri Ganganagar", ...) with zero "India" substring,
so matcher.py's is_india_job() would silently drop all of them unmodified.
Unlike Lenskart/Persistent/Lowe's (whose boards mix in genuine non-India
office locations that must NOT be relabelled), Swiggy is verified India-only:
all 79 live postings' locations are real Indian cities/towns with zero
counter-examples, and the company's own JD boilerplate self-describes as
"India's leading on-demand delivery platform" with "presence in 500+ cities
across India" and no overseas office mentioned anywhere in the dataset —
same "every posting is India" situation as firstamerican_fetcher.py. So
", India" is appended unconditionally whenever "india" isn't already present
in the location text, rather than maintaining an ever-growing town-name
whitelist (Swiggy explicitly claims 500+ India cities — any hardcoded list
would under-cover real future postings). One location value is not a city at
all: "Sumadhura Capitol Towers" is Swiggy's own tech-park office building in
Bengaluru (Whitefield/K R Puram area) and appears verbatim as `location` for
several Technology-stream postings (e.g. Staff/Senior Data Scientist roles)
— verified by opening those exact postings live and confirming
"Bangalore | Karnataka" in the rendered job body, so it is correctly swept up
by the same unconditional ", India" append.

Application URL quirk (the trickiest part of this integration): MyNextHire
renders job detail pages only inside the iframe's own client-side router —
there is no plain REST "get one job" endpoint, and the "Apply"-flow links
captured by clicking through in a real browser come back mangled (the
parent frame's postMessage handoff double-URL-encodes the query string).
Decoding one of those captured links revealed the real contract: the hash
fragment carries a `p=` parameter that is a **base64-encoded JSON blob**
describing the page context, e.g.
`{"pageType":"jd","cvSource":"careers","reqId":28594,"requester":{"id":"",
"code":"","name":""},"page":"careers","bufilter":-1,"customFields":{}}`.
Verified via headless Firefox: constructing this URL directly (no prior
click, no career_page_category, no requester identity) —
`https://swiggy.mynexthire.com/employer/jobs/careers#?p={base64(blob)}` —
renders the correct job's title, ID, location and full JD with a working
"Apply" button, for arbitrary reqIds across totally different departments
(Data Scientist/Technology and Sales Manager/Business roles both tested).
Dropping `requester`/`bufilter`/`customFields` from the blob breaks
rendering ("Oops! Something went wrong!") even though their values are
never real identity data on this path — they must be present as literal
empty/placeholder values, just not populated.
"""
from __future__ import annotations

import base64
import json
import time

import requests

_REQLIST_URL = "https://swiggy.mynexthire.com/employer/careers/reqlist/get"
_JD_BASE = "https://swiggy.mynexthire.com/employer/jobs/careers"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://careers.swiggy.com",
    "Referer": "https://careers.swiggy.com/",
}

_REQLIST_PAYLOAD = {"source": "careers", "code": "", "filterByBuId": -1}

# Module-level cache: the full board is fetched once per process and reused
# for every keyword/page call, same idiom as razorpay_fetcher.py/
# cred_fetcher.py/lenskart_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure from MyNextHire."""


def _normalize_location(raw_loc: str) -> str:
    """Append ", India" unless already present — see module docstring for
    why Swiggy's board is treated as unconditionally India (verified
    India-only, no counter-examples in the live 79-job pool)."""
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
    docstring for how this blob shape was reverse-engineered.
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
    return f"{_JD_BASE}#?p={encoded}"


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
    """Fetch the entire Swiggy MyNextHire req pool once and cache it.

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
                raise RateLimitError("Swiggy: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Swiggy cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Swiggy cache fill: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Swiggy cache fill: invalid JSON — {exc}") from exc

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
    print(f"[Swiggy] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Swiggy jobs from the cached full req pool.

    keyword/location are accepted for interface compatibility but ignored:
    MyNextHire's own frontend fetches the full unfiltered pool and does all
    keyword/location/category filtering client-side in the browser, so there
    is no reliable server-side keyword/location contract to depend on. All
    jobs (India and non-India) are returned — India scoping is left to
    matcher.py's is_india_job() / config exclude_locations, aside from the
    city-name normalisation documented in the module docstring.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Swiggy job.

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
