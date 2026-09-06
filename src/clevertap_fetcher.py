"""Fetches CleverTap job listings from its Kula.ai-hosted public career site.

ATS discovery (live, 2026-09-06): clevertap.com/careers/ links to TWO
different job-board URLs -- `https://clevertap.darwinbox.in/...` (Darwinbox)
and `https://careers.kula.ai/clevertap` (Kula, an AI-recruiting platform that
also hosts a candidate-facing careers microsite per company). The Darwinbox
link is a DEAD END for a plain `requests` fetcher: EVERY page under
`clevertap.darwinbox.in`, including the public `/ms/candidateapi/job` search
endpoint, returns a Cloudflare "Attention Required" JS challenge page (HTTP
200 with challenge HTML, not JSON) to a cold request -- confirmed not
CleverTap-specific by probing a different, previously-documented-as-open
Darwinbox tenant (`zepto.darwinbox.in`, cited as working in
zomato_fetcher.py's own investigation notes) and getting the identical
Cloudflare challenge, i.e. Darwinbox has since put its whole `candidateapi`
surface behind Cloudflare bot management platform-wide. This repo's existing
Darwinbox-behind-Cloudflare precedent (perfios_fetcher.py) needed a full
headless-Firefox Playwright session to clear that challenge.

That workaround is unnecessary here because the SECOND link
(`careers.kula.ai/clevertap`) is CleverTap's real, current, actively-used
board -- confirmed by the on-page copy ("Build whats next and grow with
CleverTap") and live titles that don't overlap with the stale Darwinbox
board -- and is NOT behind any bot-detection: a bare `requests.get()` (no
cookies, no special headers) returns 200 with the full board server-rendered
inline. This fetcher uses that page exclusively and never touches Darwinbox.

Kula's career page is a Next.js App Router page whose full job data is
embedded as a React Server Components "flight" payload inside
`<script>self.__next_f.push([1,"..."])</script>` tags -- there is no separate
plain REST endpoint for this data (checked: `api.kula.ai/careers/*` and
`api.kula.ai/public/*` are just SPA-shell catch-alls, not real routes).
Decoding this payload requires two steps, both implemented below:

1. Each `push([1,"..."])` argument is itself a valid JSON string literal
   (Next.js escapes it the standard JSON way) -- `json.loads()` on each one
   yields the raw flight-protocol text, which is concatenated in order.
2. Inside that concatenated text, the full jobs array is a plain JSON array
   literal (`[{"id":...,"account_id":...,"title":...,"ats_job":{...}}, ...]`)
   -- located by regex and parsed with `json.JSONDecoder().raw_decode()` at
   the match position (robust to nesting, unlike a manual bracket scan).
   Each job's `ats_job.job_description` is not inline; it's a flight-protocol
   backreference string like `"$24"` pointing at a separately-streamed text
   chunk formatted `24:T{hex_byte_length},{raw content}` elsewhere in the same
   concatenated text -- `_TEXT_CHUNK_RE` locates every such chunk header and
   slices exactly `hex_byte_length` characters of content after it (verified
   live: this exactly reaches the next chunk's header with no drift).

Verified live: 17 total postings, 11 in India (all Mumbai/Gurugram) via
`ats_job.offices[].location`, which is already a clean "City, State, India"
string needing no city-whitelist normalisation. `launch_at` (ISO-8601) is a
genuine per-job creation date. No keyword/location query param exists on
this page at all (it's a single static-per-request SSR listing filtered
client-side by the page's own facet UI), so -- same "cache-once" family as
every other Greenhouse/Lever/Ashby/Workable board in this repo -- the whole
board is parsed once per process and cached.

This flight-payload format is materially more fragile than a documented REST
API (it's an internal framework serialization, not a public contract), so
every extraction step degrades to an empty result (never a crash) if Kula
ships a page structure change; only genuine network failures raise
RateLimitError.

require_tech_in_description is NOT enabled -- live India titles are direct,
specific engineering roles ("Senior Backend Engineer", "Senior DevOps
Engineer", "Backend Engineer - Workflow Automation (Java/Temporal)") rather
than generic IT-services level bands.
"""
from __future__ import annotations

import json
import re
import time

import requests

_CAREERS_URL = "https://careers.kula.ai/clevertap"
_JOB_PAGE_BASE = "https://careers.kula.ai/clevertap"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)
_JOBS_ARRAY_START_RE = re.compile(r'\[\{"id":\d+,"account_id":\d+,"title":')
_TEXT_CHUNK_RE = re.compile(r'(?:^|\n)([0-9a-fA-F]+):T([0-9a-fA-F]+),')
_TAG_RE = re.compile(r"<[^>]+>")

# Module-level cache: the page returns the identical full board for every
# request (client-side-only filtering) -- fetch/parse it once and slice
# after.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _get(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"CleverTap {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"CleverTap {what} failed: {exc}") from exc
    raise RateLimitError(f"CleverTap {what}: no response -- {last_exc}")


def _strip_html(raw: str) -> str:
    return " ".join(_TAG_RE.sub(" ", raw or "").split())


def _decode_flight_text(html: str) -> str:
    """Concatenate every `self.__next_f.push([1,"..."])` argument's decoded
    string, in document order, into one flight-protocol text blob."""
    parts = []
    for m in _PUSH_RE.finditer(html):
        try:
            parts.append(json.loads(m.group(1)))
        except (ValueError, TypeError):
            continue
    return "".join(parts)


def _build_text_chunk_map(flight_text: str) -> dict[str, str]:
    """Map flight-protocol text-chunk id -> decoded content.

    Chunks are declared `{id}:T{hex_len},{content}` -- content runs for
    exactly hex_len characters right after the comma (verified live: this
    lands exactly on the next chunk header with no drift)."""
    chunks: dict[str, str] = {}
    for m in _TEXT_CHUNK_RE.finditer(flight_text):
        chunk_id, hex_len = m.group(1), m.group(2)
        try:
            length = int(hex_len, 16)
        except ValueError:
            continue
        start = m.end()
        chunks[chunk_id] = flight_text[start:start + length]
    return chunks


def _resolve_description(ref: str, chunk_map: dict[str, str]) -> str:
    """`ats_job.job_description` is a flight backreference like "$24"."""
    if not ref or not ref.startswith("$"):
        return _strip_html(ref)
    return _strip_html(chunk_map.get(ref[1:], ""))


def _location_from_offices(offices: list) -> str:
    parts = []
    for o in offices or []:
        loc = (o.get("location") or o.get("name") or "").strip()
        if loc and loc not in parts:
            parts.append(loc)
    return "; ".join(parts)


def _fill_cache(timeout: int = 20) -> None:
    """Fetch and parse the entire CleverTap (Kula) board once; cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent lesson -- see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = _get(_CAREERS_URL, timeout, "career page fetch")
    flight_text = _decode_flight_text(r.text)
    if not flight_text:
        print("[CleverTap] Cache filled: 0 total jobs (flight payload not found)")
        return

    m = _JOBS_ARRAY_START_RE.search(flight_text)
    if not m:
        print("[CleverTap] Cache filled: 0 total jobs (jobs array not found)")
        return

    try:
        raw_jobs, _ = json.JSONDecoder().raw_decode(flight_text, m.start())
    except (ValueError, TypeError) as exc:
        print(f"[CleverTap] Cache filled: 0 total jobs (jobs array parse error: {exc})")
        return

    chunk_map = _build_text_chunk_map(flight_text)

    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "").strip()
        title = (j.get("title") or "").strip()
        if not (job_id and title):
            continue
        ats_job = j.get("ats_job") or {}
        location = _location_from_offices(ats_job.get("offices")) or "India"
        posting_date = (j.get("launch_at") or "")[:10]

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": posting_date,
            "application_url": f"{_JOB_PAGE_BASE}/{job_id}",
        })
        _desc_cache[job_id] = _resolve_description(
            ats_job.get("job_description") or "", chunk_map
        )

    _job_cache[:] = collected
    print(f"[CleverTap] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of CleverTap (Kula-hosted) postings.

    keyword/location are accepted for interface compatibility but ignored --
    the page has no query param for either; it always server-renders the
    identical full board, filtered only by client-side facet UI. The shared
    matcher does the real title/skill/India filtering. The whole board is
    parsed once per process.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start: start + num]


def _job_id_from_url(application_url: str) -> str:
    return (application_url or "").rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single CleverTap job.

    Served entirely from the cache filled by fetch_jobs()/_fill_cache() --
    the career page's flight payload already carries every job's full
    description text, so no separate per-job HTTP request is made.
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
