"""Fetches DAZN job listings via the Pinpoint ATS public JSON board.

DAZN's branded careers site (careers.dazn.com, India landing page at
careers.dazn.com/india) is a JS SPA — the job list is not server-rendered
(confirmed: 0 occurrences of "postings/" in the raw HTML). DevTools-style
inspection of the page's embedded feature-flag JSON (fields like
"ai_custom_fields_via_arti", "willo_video_interviewing", "nova_scoring",
"pinpoint_checks_beta") identified the underlying ATS as **Pinpoint**
(pinpointhq.com) — a new ATS vendor for this repo, not previously seen.

Pinpoint publishes an open, unauthenticated JSON endpoint at
`https://{tenant}.pinpointhq.com/postings.json` (documented at
developers.pinpointhq.com as the "Job Postings JSON Endpoint", explicitly
designed for client-side/no-CORS-issue fetching). DAZN's tenant slug is
"dazn" (verified live 2026-09-03: `https://dazn.pinpointhq.com/postings.json`
returns HTTP 200 with `{"data": [...]}`, 117 postings globally). No API key
needed (contrast with the authenticated `/api/v1/jobs` endpoint, which 401s
without an `X-API-KEY` header — not used here). `?page=N` is ignored — the
endpoint always returns the same full global board in one call, same
cache-once shape as CRED/Meesho/Paytm's Lever boards.

India: 47 of 117 postings have a `location.name` containing "india" as a
plain substring, but one of those is genuinely **"US - Indiana"** (Account
Executive) — the exact Indianapolis/Indiana false-positive risk already
documented for PayPal/FactSet in the playbook. Filtered here with a
word-boundary `\\bindia\\b` regex (46 genuine India postings — all
Hyderabad), rather than relying on matcher.py's own plain-substring
`is_india_job()`, which does NOT have this word-boundary protection and
would otherwise let "Indiana" through as a false India match. Genuine India
locations already read "India - Hyderabad" verbatim, so no ", India"
suffix-appending is needed (unlike Lever-based fetchers).

Description: `description` + `key_responsibilities` +
`skills_knowledge_expertise` HTML fields are concatenated and stripped —
all three carry real skill-bearing content (e.g. the requirements bullets
live in `skills_knowledge_expertise`, not `description` alone). Entities are
single-encoded (`&nbsp;` etc.) — no Groww/Razorpay-style double-escaping
here. Full descriptions are already inline in the one cached board response,
so `fetch_job_description()` needs no separate HTTP call, same as
CRED/Meesho/Paytm/Groww/Razorpay.

Posting date: no date field of any kind (`created_at`/`published_at`/
`live_at`) is exposed anywhere in `postings.json` — same "no posting-date
field exposed anywhere" situation as IBM. `posting_date` is always "".

Live content check (2026-09-03): DAZN Hyderabad's current real software
roles are overwhelmingly Node.js/TypeScript/AWS/Swift-flavored (e.g. "Senior
Software Engineer, IN" — Node.js/TypeScript/NestJS/DynamoDB backend; "iOS
Lead" — Swift) with zero current `.NET/C#` signal anywhere in the India
pool. The one posting with genuine `AI / ML / Python`-track content
("Conversational Developer" — explicit "Generative AI (LLMs)", "Agentic AI
systems", "prompt engineering", OpenAI GPT/Claude, Python scripting) does
not pass the shared `title_family` check (no title_family phrase matches
"Conversational Developer") — a real near-miss, not a fetcher defect;
flagged here per the "flag, don't silently patch" discipline rather than
touched, since title_family/exclude_terms are shared global config. A
genuine current zero real matches is therefore expected on first run, same
class as Disney/GE Aerospace/Nutanix/Schneider Electric.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_URL = "https://dazn.pinpointhq.com/postings.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://careers.dazn.com/",
}

_INDIA_RE = re.compile(r"\bindia\b", re.IGNORECASE)

# Module-level cache: the Pinpoint postings.json endpoint returns the same
# full global board for every call (no working keyword/location/page
# params), so fetch it once and slice/look up afterward — same shape as
# cred_fetcher.py/paytm_fetcher.py.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _build_description(posting: dict) -> str:
    parts = [
        _strip_html(posting.get("description") or ""),
        _strip_html(posting.get("key_responsibilities") or ""),
        _strip_html(posting.get("skills_knowledge_expertise") or ""),
    ]
    return " ".join(p.strip() for p in parts if p and p.strip())


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Pinpoint board once; cache India job list + descriptions.

    _cache_filled is set before the request attempt so a transient failure
    doesn't trigger a retry storm on every fetch_jobs() call made during the
    same process run (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(_BOARD_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("DAZN Pinpoint board: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"DAZN board fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"DAZN board fetch: no response — {last_exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RateLimitError(f"DAZN board fetch: invalid JSON — {exc}") from exc

    postings = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(postings, list):
        return

    jobs: list[dict] = []
    for p in postings:
        job_id = str(p.get("id") or "").strip()
        title = (p.get("title") or "").strip()
        application_url = (p.get("url") or "").strip()
        loc = p.get("location") or {}
        loc_name = (loc.get("name") or "").strip()
        if not (job_id and title and application_url and loc_name):
            continue
        # Word-boundary India check — a plain substring match would also
        # accept "US - Indiana" (Account Executive), which is genuinely not
        # India. See PayPal/FactSet's identical Indianapolis/Indiana lesson.
        if not _INDIA_RE.search(loc_name):
            continue

        jobs.append({
            "id": job_id,
            "title": title,
            "location": loc_name,
            "posting_date": "",  # no date field exposed anywhere on this ATS
            "application_url": application_url,
        })
        _desc_cache[job_id] = _build_description(p)

    _job_cache[:] = jobs
    print(f"[DAZN] Cache filled: {len(jobs)} India jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of DAZN (Pinpoint) India postings.

    Keyword/location are accepted but ignored — the public postings.json
    endpoint returns the identical full global board regardless of query
    params (no server-side filtering exists to call); the shared matcher
    does the real title/skill filtering. The India subset is cached once
    and sliced here.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    """url is .../postings/{uuid}; find the cached job whose own url matches."""
    for job in _job_cache:
        if job["application_url"] == application_url:
            return job["id"]
    return ""


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single DAZN job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() —
    Pinpoint's postings.json already carries the full description, so there
    is no separate detail endpoint to call.
    """
    _fill_cache(timeout=timeout)
    job_id = _job_id_from_url(application_url)
    description = _desc_cache.get(job_id, "")
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    return description, posting_date
