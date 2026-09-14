"""Fetches Tiger Analytics job listings via the Zwayam ATS.

careers.tigeranalytics.com is an Angular SPA that looks bespoke at a glance
("tigeranalytics" folder, no obvious vendor branding), but the response's
own `content-security-policy` header lists `*.openings.co` and `*.zwayam.com`
as allowed frame-ancestors, and the Angular bundle (`main.*.js`) hardcodes
`APIENDPOINT: "https://public.zwayam.com/"` plus a base64 `COMPANYID`
(`"MTYyMDE="` -> `"16201"`) — confirmed live via direct request. Same shared
multi-tenant `public.zwayam.com` endpoint and request shape as CRISIL
(`crisil_fetcher.py`) and the same ATS family as Persistent/eClerx
("openings.co" appears to be a newer white-label front-end Zwayam offers,
this repo's first sighting of that branding).

Key quirks (confirmed via live requests, not assumed):
- `POST /jobs/search` with a multipart `filterCri` JSON field
  (`paginationStartNo`, `sortCriteria`, `anyOfTheseWords`) plus `domain`
  (`careers.tigeranalytics.com`) and `companyId` (base64 `MTYyMDE=`) —
  identical shape to CRISIL/Persistent. Server page size is a fixed 10;
  `hasMoreData` signals when to stop.
- `anyOfTheseWords` is not exercised here — the full ~106-job board is
  cached once and matcher.py's title/skill filters do the real work (same
  choice as CRISIL/Persistent).
- Unlike CRISIL, `locationSeparatedbySlash`/`location` are often a
  period-joined city list with NO state/country context at all
  ("Chennai.Hyderabad.Bangalore.Remote") — cannot be trusted alone. The
  structured `jobLocationRecord` list (each entry has its own
  `formattedLocation` like "Bengaluru, Karnataka, India") is authoritative
  and used instead, joined the same way `locationSeparatedbySlash` values
  are elsewhere in this repo. Falls back to the slash/period fields only if
  `jobLocationRecord` is empty.
- Full description lives behind `POST /jobs-service/v1/jobs/careersite`
  with `{jobUrl, externalSource: "CAREERSITE", campusUrl: "empty",
  companyId}` (companyId here is the *decoded* "16201", not the base64
  form used in the search request) — identical to CRISIL's detail call.
  Detail page URL: `https://careers.tigeranalytics.com/tigeranalytics/#!/job-view/{jobUrl}`
  (Angular hashbang route, confirmed against real "View Job" links on the
  live site).
- Tiger Analytics's board mixes lots of multi-city combo postings
  (Chennai/Hyderabad/Bengaluru/Pune together) — same "combo posting
  excluded entirely because Pune/Chennai is also listed" tradeoff already
  documented for CRISIL/Eurofins; not fixed here.

Live-verified 2026-09-13: 106 total postings on the board (all India — this
is Tiger Analytics's dedicated India careers site, no global mixing).
Genuine AI/ML matches exist: "Senior AI/ML Engineer - Azure AI" and
"Senior AI/ML Engineer - Oracle AI" (Chennai/Hyderabad/Bengaluru combo
postings) explicitly name Python/LLM/generative-AI stacks in the JD body.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime
from json import dumps as _json_dumps

import requests

_CAREERS_BASE = "https://careers.tigeranalytics.com/tigeranalytics"
_API_BASE = "https://public.zwayam.com"
_SEARCH_URL = f"{_API_BASE}/jobs/search"
_DETAIL_URL = f"{_API_BASE}/jobs-service/v1/jobs/careersite"
_COMPANY_ID_B64 = "MTYyMDE="  # base64("16201")
_COMPANY_ID = "16201"
_DOMAIN = "careers.tigeranalytics.com"
_MAX_PAGES = 100  # safety ceiling; server page size is a fixed 10 (~106 jobs)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_CAREERS_BASE}/",
    "Origin": "https://careers.tigeranalytics.com",
}

# Module-level cache: filled once, reused for all keyword calls.
_india_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert 'DD-Mon-YYYY' (e.g. '24-Sep-2025') -> 'YYYY-MM-DD'."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _location_from_job(src: dict) -> str:
    """Prefer structured jobLocationRecord (has real state/country context)
    over locationSeparatedbySlash/location, which on this tenant are often
    a bare period-joined city list with no country ("Chennai.Hyderabad").
    """
    records = src.get("jobLocationRecord") or []
    formatted = [r.get("formattedLocation") for r in records if r.get("formattedLocation")]
    if formatted:
        return " / ".join(dict.fromkeys(formatted))  # de-dupe, preserve order

    loc = src.get("locationSeparatedbySlash")
    if loc and "india" in loc.lower():
        return loc
    return (src.get("location") or "").replace(";", " / ")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through every Tiger Analytics posting once and cache India ones.

    _cache_filled is set before the loop so a failure doesn't trigger a
    retry storm on every subsequent keyword call (Honeywell lesson).
    """
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    start = 0
    for page_num in range(_MAX_PAGES):
        if page_num > 0:
            time.sleep(0.15)

        filter_cri = {
            "paginationStartNo": start,
            "selectedCall": "sort",
            "sortCriteria": {"name": "modifiedDate", "isAscending": False},
            "anyOfTheseWords": "",
        }
        files = {
            "filterCri": (None, _json_dumps(filter_cri)),
            "domain": (None, _DOMAIN),
            "companyId": (None, _COMPANY_ID_B64),
        }

        last_exc: Exception | None = None
        r = None
        for attempt in range(3):
            try:
                r = requests.post(_SEARCH_URL, headers=_HEADERS, files=files, timeout=timeout)
                if r.status_code == 429:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise RateLimitError("Tiger Analytics: 429 rate-limited during cache fill")
                r.raise_for_status()
                break
            except RateLimitError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Tiger Analytics cache fill failed: {exc}") from exc

        if r is None:
            raise RateLimitError(f"Tiger Analytics cache fill: no response — {last_exc}")

        payload = r.json().get("data", {})
        batch = payload.get("data", [])
        if not batch:
            break

        for item in batch:
            src = item.get("_source", {})
            location = _location_from_job(src)
            if "india" not in location.lower():
                continue
            job_id = src.get("id")
            title = (src.get("jobTitle") or "").strip()
            job_url = src.get("jobUrl") or ""
            if not (job_id and title and job_url):
                continue
            collected.append({
                "id": str(job_id),
                "title": title,
                "location": location,
                "posting_date": _parse_date(src.get("createDate") or ""),
                "application_url": f"{_CAREERS_BASE}/#!/job-view/{job_url}",
            })

        start += len(batch)
        if not payload.get("hasMoreData"):
            break

    _india_cache = collected
    print(f"[Tiger Analytics] Cache filled: {len(collected)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Tiger Analytics India jobs.

    Keywords are ignored — the shared title/skill filters in matcher.py do
    the real work. The full India pool is cached once (see _fill_cache).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single Tiger Analytics job."""
    job_url = application_url.rsplit("/job-view/", 1)[-1]

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(
                _DETAIL_URL,
                headers={**_HEADERS, "Content-Type": "application/json"},
                json={
                    "jobUrl": job_url,
                    "externalSource": "CAREERSITE",
                    "campusUrl": "empty",
                    "companyId": _COMPANY_ID,
                },
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Tiger Analytics description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Tiger Analytics description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Tiger Analytics description fetch: no response — {last_exc}")

    data = r.json()
    description = _strip_html(data.get("longDescription", ""))
    posting_date = _parse_date(data.get("createDate", "") or "")
    return description, posting_date
