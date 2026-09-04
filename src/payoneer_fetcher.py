"""Fetches Payoneer job listings via the Greenhouse ATS.

Verified from scratch (2026-09-04): `payoneer.com/careers` (redirects to
`careers.payoneer.com`) itself is a bot-protected obfuscated-JS shell (a
`server: rhino-core-shield`-fronted domain that returns a JS challenge page
to plain HTTP, not job data) — but its own `robots.txt` names a real
sitemap at `https://payoneer.teamme.link/sitemap.xml`. That "TeamMe"
career-site (a Next.js app, `payoneer.teamme.link`) IS plain-HTTP
accessible and server-renders (via a React Server Components payload
embedded in the initial HTML, no JS execution needed) the full job list —
and every job's `applyUrl` field looks like
`https://www.payoneer.com/careers/position/{id}/?gh_jid={id}`. Fetching one
of those application URLs directly shows a page whose Content-Security-
Policy allowlists `*.greenhouse.io` and whose HTML embeds
`greenhouse.io/embed/job_board/js?for=payoneer` — confirming the real ATS
is **Greenhouse**, board token `payoneer`, with the TeamMe site and the
`payoneer.com/careers/position/...` pages both acting as branded frontends
over it. This is a genuinely new pattern for this repo (a third-party
"TeamMe" career-site builder as one more layer over a standard Greenhouse
board) but the actual data source is the same well-known, already-
integrated ATS as Groww/Razorpay/CRED/Meesho/Paytm/Definitive Healthcare.

Public boards API:
GET https://boards-api.greenhouse.io/v1/boards/payoneer/jobs?content=true
returns Payoneer's entire GLOBAL board (123 postings as of verification;
unlike Definitive Healthcare's India-only board) in one call, including
each job's full HTML `content` field — no per-job detail fetch needed.
Same "ignores query params, fetch-once cache" pattern as every other
Greenhouse board in this repo. `_cache_filled` is set *before* the fetch
attempt (Honeywell lesson) so a transient failure doesn't retry-storm on
every subsequent call within the same process.

India filtering is done INSIDE the cache fill (case-insensitive "india"
substring on `location.name`), same as Groww/Razorpay/CRED/Meesho/Paytm.
Every India posting observed on this board already carries a clean
"Bangalore, India" / "Gurugram, India" location string (confirmed across
the full 123-job board) — no city-only strings needing a whitelist, and
no Chennai/Pune/Tamil Nadu/Kochi/etc. India office observed at all (just
Bangalore and Gurugram), so no `exclude_locations` leakage risk to guard
against here beyond the shared config list.

There is no separate posting-date field; `first_published` (an
undocumented but genuinely earlier and more accurate field than
`updated_at`, same discovery as Groww/Razorpay) is used when present,
falling back to `updated_at` (both truncated to YYYY-MM-DD).

Content quirk: this board's `content` field is HTML-entity-escaped one
level (the raw string is literally "&lt;div class=&quot;...&quot;&gt;..."
rather than "<div class=\"...\">...") — NOT double-escaped like Groww/
Razorpay/Paytm. `_strip_html` unescapes once before stripping tags and
unescapes again afterward for any inline entities in the actual text
(e.g. "&amp;nbsp;") — a no-op on the second pass for a single-escaped
board like this one, so the same helper is safe either way and reused
as-is rather than writing a Payoneer-specific variant.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_BOARD_TOKEN = "payoneer"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"
_FALLBACK_JOB_BASE = f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Module-level cache: the full GLOBAL board is fetched once, filtered to
# India-located postings, and reused for every keyword/page call
# (Greenhouse's public boards API ignores query params other than `content`).
_job_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the full Payoneer Greenhouse board once, cache India postings.

    _cache_filled is set to True before the fetch attempt so a failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() /
    fetch_job_description() call within the same process (Honeywell
    lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(
                _LIST_URL,
                headers=_HEADERS,
                params={"content": "true"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(
                    "Payoneer: 429 rate-limited during cache fill"
                )
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Payoneer cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"Payoneer cache fill: no response — {last_exc}")

    raw_jobs = r.json().get("jobs", [])
    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc = ((j.get("location") or {}).get("name") or "").strip()
        if "india" not in loc.lower():
            continue

        posting_date = (j.get("first_published") or j.get("updated_at") or "")[:10]

        app_url = j.get("absolute_url") or f"{_FALLBACK_JOB_BASE}/{job_id}"

        _content_cache[job_id] = j.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": posting_date,
            "application_url": app_url,
        })

    _job_cache = collected
    print(f"[Payoneer] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} global)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Payoneer India jobs from the cached, filtered board.

    keyword/location are accepted for interface compatibility but ignored:
    Greenhouse's public boards API returns the same full global board
    regardless of query params, and India scoping is already applied
    during the cache fill (see _fill_cache).
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Payoneer job.

    Served from the cache filled by _fill_cache() when possible (Greenhouse's
    list response already includes full HTML `content` with
    `?content=true`). Falls back to the single-job detail endpoint for a job
    ID that isn't cached (cache not yet filled, or the job closed/moved
    after caching).
    """
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").split("?")[0].rstrip("/").split("/")[-1]

    if job_id in _content_cache:
        description = _strip_html(_content_cache[job_id])
        posting_date = ""
        for job in _job_cache:
            if job["id"] == job_id:
                posting_date = job["posting_date"]
                break
        return description, posting_date

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(
                _DETAIL_URL.format(job_id=job_id),
                headers=_HEADERS,
                params={"content": "true"},
                timeout=timeout,
            )
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Payoneer: 429 rate-limited fetching job {job_id}")
            r.raise_for_status()
            data = r.json()
            description = _strip_html(data.get("content") or "")
            posting_date = (data.get("first_published") or data.get("updated_at") or "")[:10]
            return description, posting_date
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

    raise RateLimitError(f"Payoneer: failed to fetch job {job_id} detail — {last_exc}")
