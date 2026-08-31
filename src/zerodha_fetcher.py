"""Fetches Zerodha job listings from careers.zerodha.com's own JSON API.

ATS discovery (confirmed live 2026-08-31): careers.zerodha.com is a small
Vite/Vue 3 single-page app (`/assets/index-*.js`) — not one of the vendors
in the PLAYBOOK's Common ATS table. The page itself is a ~1KB shell that
mounts `#app`; its bundled JS makes exactly one same-origin call to render
the whole page:

    GET https://careers.zerodha.com/api/jobs   -> {"count", "data", "success"}

confirmed by decompiling the shipped bundle (`M=async()=>{... const e=await
fetch("/api/jobs") ...`) — no query params of any kind are ever sent by the
frontend (no keyword/location/department/pagination param exists to send).
`connect-src 'self'` in the page's CSP also rules out any third-party ATS
being called from the client. Response shape: `{"count": int, "data": [...],
"success": bool}`. `Allow: HEAD, GET, OPTIONS` confirms it's read-only.

Field mapping — derived directly from the production bundle's Vue template
bindings (not from a live sample job, see "Known-empty board" below):
  - `name`         -> unique id (Zerodha's backend looks Frappe-flavoured —
                      "name" as the primary key, old snapshots of this same
                      domain shipped erpnext-web.min.js/frappe-web.min.js —
                      and this is exactly the field the app itself uses as
                      the row key (`k.value===e.name`) and passes as the
                      application form's `jobId` prop)
  - `job_title`    -> title (bound to the `<h3>`, and passed as the
                      application form's `jobTitle` prop)
  - `location`     -> office/city text (bound next to a pin icon)
  - `location_type`-> work-arrangement qualifier shown as "{location} -
                      {location_type}" (e.g. likely Remote/Hybrid/Onsite) —
                      folded into the location string the same way when
                      present, since matcher.py only reads one location
                      field
  - `description`  -> full HTML, rendered via `innerHTML` straight from the
                      list payload with **no separate detail endpoint at
                      all** — confirmed inline, same as Amazon/Groww/Paytm
  - No posting-date field is referenced anywhere in the bundle (no
                      "date"/"posted"/"created"/"modified" identifier
                      appears in the whole ~58KB file). `fetch_jobs` still
                      defensively probes a few plausible Frappe-style key
                      names (`posting_date`, `creation`, `modified`) in case
                      the API returns more than the frontend consumes, but
                      falls back to "" like `ibm_fetcher.py` does for the
                      same reason.

Known-empty board (verified, not a fetcher bug): live `GET /api/jobs`
returns `{"count":0,"data":[],"success":true}` right now, and the bundle
itself hardcodes a "There are no job openings currently." empty state —
i.e. the app's own author expected and coded for this. Cross-checked
against the Wayback Machine: 3 independent captures of this exact endpoint
(2026-02-19, 2026-08-04, 2026-08-21) all show the same empty payload, so
Zerodha's board has genuinely had zero open roles for 6+ months, not a
transient blip. Because no live job object exists to inspect, this
module's field mapping is verified against the site's own executing
JavaScript rather than a live sample dict (the strongest evidence
available short of a real posting appearing) — re-verify field names
against a real job the next time this board is non-empty.

No per-job URL exists (worth flagging for the PLAYBOOK): unlike the old
Frappe-era site (which had real per-job pages like
`/jobs/customer-support-specialist`), this Vue rewrite has *no client-side
routing at all* — no `URLSearchParams`/`location.hash`/router of any kind
in the bundle. The whole UI is one static page: clicking a listing expands
an inline accordion, and "Apply" opens an in-page modal — there is no
distinct navigable URL per job. `application_url` is therefore synthesised
as `https://careers.zerodha.com/#job/{name}` (same "harmless fragment on
the single real page" idiom as `mphasis_fetcher.py`'s
`#detail/job/{id}`) purely so `fetch_job_description` can look the job back
up in-cache; the fragment has no effect on the page itself, but a user who
clicks it still lands on the real, current careers page and can find/apply
to the role from there.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_API_URL = "https://careers.zerodha.com/api/jobs"
_CAREERS_PAGE = "https://careers.zerodha.com/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": _CAREERS_PAGE,
}

# Zerodha is a domestic-only Indian stockbroking/fintech company (SEBI-
# regulated, no known international offices) headquartered in Bengaluru —
# so, like lenskart_fetcher.py/mphasis_fetcher.py/persistent_fetcher.py,
# bare city names are normalised to include ", India" only for recognised
# India city/region tokens, leaving anything unrecognised untouched so
# matcher.py's is_india_job() can still reject genuine non-India text
# rather than everything being blindly relabelled.
_INDIA_CITIES = (
    "bangalore", "bengaluru", "mumbai", "pune", "chennai", "hyderabad",
    "delhi", "gurugram", "gurgaon", "noida", "kolkata", "kochi",
    "trivandrum", "chandigarh", "indore", "nagpur", "lucknow", "madurai",
    "karnataka", "remote",
)

# Module-level cache: the frontend's own fetch("/api/jobs") call takes no
# params at all, so the whole board is fetched once and reused for every
# keyword/page call — same "cache-once" idiom as groww_fetcher.py /
# razorpay_fetcher.py / lenskart_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _is_india_location(loc: str) -> bool:
    low = loc.lower()
    return any(token in low for token in _INDIA_CITIES)


def _normalize_location(raw_location: str, location_type: str) -> str:
    loc = (raw_location or "").strip()
    loc_type = (location_type or "").strip()
    combined = f"{loc} - {loc_type}" if loc and loc_type else loc
    if combined and "india" not in combined.lower() and _is_india_location(combined):
        return f"{combined}, India"
    return combined


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _extract_posting_date(job: dict) -> str:
    """Best-effort date extraction.

    No date field is referenced anywhere in the production bundle (see
    module docstring), so this only guards against the API quietly
    returning more than the frontend consumes. Falls back to "" like
    ibm_fetcher.py does when a company's ATS exposes no date at all.
    """
    for key in ("posting_date", "posted_at", "creation", "modified", "created_at"):
        raw = job.get(key)
        if raw and isinstance(raw, str):
            return raw[:10]
    return ""


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Zerodha board once and cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent
    fetch_jobs()/fetch_job_description() call within the same process
    (Honeywell/Persistent lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_API_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Zerodha: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zerodha cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Zerodha cache fill: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"Zerodha cache fill: invalid JSON — {exc}") from exc

    if not payload.get("success", False):
        raise RateLimitError("Zerodha cache fill: API reported success=false")

    raw_jobs = payload.get("data") or []
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("name") or "").strip()
        title = (j.get("job_title") or "").strip()
        if not (job_id and title):
            continue

        loc = _normalize_location(j.get("location") or "", j.get("location_type") or "")
        posting_date = _extract_posting_date(j)
        app_url = f"{_CAREERS_PAGE}#job/{job_id}"

        _desc_cache[job_id] = j.get("description") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache[:] = collected
    print(f"[Zerodha] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Zerodha jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    the site's own frontend calls `fetch("/api/jobs")` with zero params —
    there is no keyword/location/department/pagination contract to rely on
    server-side — so the (currently tiny/empty) full pool is cached once
    and paginated client-side here instead.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    m = re.search(r"#job/(\S+)$", application_url or "")
    return m.group(1) if m else ""


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Zerodha job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    the board's jobs response already includes each job's full HTML
    description inline, so no separate detail HTTP call is made.
    """
    _fill_cache(timeout=timeout)

    job_id = _job_id_from_url(application_url)
    description = _strip_html(_desc_cache.get(job_id, ""))

    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break

    return description, posting_date
