r"""Fetches Yubi (formerly CredAvenue) job listings — Zoho Recruit ATS.

ATS discovery (2026-08-31): www.go-yubi.com/careers embeds job cards
linking to `go-yubi.zohorecruit.in/jobs/Careers/{id}/{slug}` — Zoho Recruit,
a new ATS vendor for this repo. The public careers listing page is fully
server-rendered (no separate XHR/API call — confirmed via live Playwright
network capture, only one response for the whole page): the entire board
is embedded as an HTML-entity-encoded JSON array inside one hidden input:

    GET https://go-yubi.zohorecruit.in/jobs/Careers
        -> <input type="hidden" id="jobs" value="[{&#34;Company&#34;:...}]">

Decode: `html.unescape()` the attribute value, then `json.loads()` — no
Playwright needed, a plain `requests.get()` with a browser User-Agent
returns the identical HTML. Confirmed live: 275 postings across every
"Company" (Yubi group brand) on this one shared Zoho Recruit portal —
Yubi, Yubi Markets, Aspero, Accumn, Spocto, YuCollect — kept as-is, no
per-brand filtering, same policy as CRED/Prefr elsewhere in this repo.
Each record on the list page has `Job_Opening_Name`/`Posting_Title`, `id`,
`City`, `Country`, `Company` — but no description or posting-date field,
so those are filled in during the per-job detail fetch.

**Two-layer escaping on the job-DETAIL page — the one real gotcha here.**
The list page's hidden input uses plain HTML-entity encoding (`&#34;` for
`"`), decoded with one `html.unescape()` call. The per-job detail page
embeds its (single-job) JSON payload differently:

    <script>var jobs = JSON.parse('[{\\x22Company\\x22:...}]');</script>

This is a JS *single-quoted string literal* containing JSON text where
every literal `"` was replaced with the JS hex-escape `\x22` (and
backslashes doubled, per standard JS string-literal escaping) — NOT HTML
entities. Decoding this correctly requires replaying actual JS string-
literal escape semantics (`\xHH` -> that byte, `\\` -> single backslash,
any other `\X` -> just `X`, escapes processed left-to-right so a run like
`\\\x22` correctly resolves to `\"` — the standard JSON in-string escaped
quote — not to `\` + `"` + `22` or any other misparse). A naive single
find/replace of `\x22` -> `"` breaks on the doubly-escaped inner quotes
(e.g. `id=\"spandesc\"` inside the HTML-formatted `Job_Description` field
comes through as three literal backslashes then `x22`, not one). Verified
correct against a real job (`Full Stack Software Engineer`,
id 66789000031544005) end to end: decode -> `json.loads` -> real HTML
`Job_Description` -> tag-stripped -> readable text.

Location: `City`/`Country` fields are already clean and authoritative
("Bangalore South", "India") — no substring guessing needed, unlike most
other ATSes in this repo.

Application URL: `https://go-yubi.zohorecruit.in/jobs/Careers/{id}` (the
title-slug segment is decorative — confirmed the bare numeric-ID URL
returns HTTP 200 and renders the correct job).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_LIST_URL = "https://go-yubi.zohorecruit.in/jobs/Careers"
_JOB_PAGE_BASE = "https://go-yubi.zohorecruit.in/jobs/Careers/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_LIST_INPUT_MARKER = '<input type="hidden" value="[{&#34;Company&#34;'
_DETAIL_SCRIPT_MARKER = "var jobs = JSON.parse('"

# Module-level cache: the list endpoint has no query params and always
# returns the full board, so fetch once per process.
_job_cache: list[dict] = []
_detail_cache: dict[str, dict] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _js_string_unescape(s: str) -> str:
    """Decode a JS single-quoted string literal's backslash escapes.

    Handles `\\xHH` (hex escape -> that char), `\\\\` (-> single backslash),
    and any other `\\X` (-> just X, per ECMAScript's NonEscapeCharacter
    rule — the backslash is silently dropped). Processed left-to-right via
    regex so multi-backslash runs like `\\\\\\x22` correctly resolve to the
    JSON in-string escaped-quote sequence `\\"` rather than being misparsed.
    See module docstring for why this two-layer scheme exists.
    """
    def repl(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        return m.group(2)

    return re.sub(r"\\x([0-9a-fA-F]{2})|\\(.)", repl, s)


def _location_from_job(j: dict) -> str:
    city = (j.get("City") or "").strip()
    country = (j.get("Country") or "").strip()
    if city and country:
        if country.lower() in city.lower():
            return city
        return f"{city}, {country}"
    return city or country or "India"


def _get_html(url: str, *, timeout: int = 20, context: str = "") -> str:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Yubi {context}: 429 rate-limited")
            r.raise_for_status()
            return r.text
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Yubi {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Yubi {context}: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Yubi (Zoho Recruit) board once and cache it.

    _cache_filled is set before the fetch attempt so a transient failure
    doesn't retry-storm on every subsequent fetch_jobs() call in the same
    process (Honeywell/Persistent lesson).
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    html_text = _get_html(_LIST_URL, timeout=timeout, context="job list")

    idx = html_text.find(_LIST_INPUT_MARKER)
    if idx == -1:
        print("[Yubi] Cache fill: jobs hidden-input marker not found — 0 jobs")
        return
    val_start = idx + len('<input type="hidden" value="')
    val_end = html_text.find('" id="jobs"', val_start)
    if val_end == -1:
        print("[Yubi] Cache fill: could not locate end of jobs input — 0 jobs")
        return

    blob = html_mod.unescape(html_text[val_start:val_end])
    try:
        raw_jobs = json.loads(blob)
    except ValueError as exc:
        raise RateLimitError(f"Yubi job list: invalid JSON — {exc}") from exc

    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "").strip()
        title = (j.get("Posting_Title") or j.get("Job_Opening_Name") or "").strip()
        if not (job_id and title):
            continue
        collected.append({
            "id": job_id,
            "title": title,
            "location": _location_from_job(j),
            "posting_date": "",  # not on the list page; filled on detail fetch
            "application_url": f"{_JOB_PAGE_BASE}{job_id}",
        })

    _job_cache[:] = collected
    print(f"[Yubi] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Yubi (Zoho Recruit) jobs from the cached full board.

    keyword/location are accepted for interface compatibility but ignored:
    the list page has no query params and always returns the full board;
    the shared matcher does the real title/skill/India filtering.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Yubi job.

    Descriptions are NOT on the list page — a separate detail-page fetch is
    required, whose JS-string-escaped payload needs the decode documented
    in the module docstring.
    """
    job_id = application_url.rstrip("/").rsplit("/", 1)[-1]

    detail = _detail_cache.get(job_id)
    if detail is None:
        html_text = _get_html(application_url, timeout=timeout, context=f"job detail {job_id}")
        idx = html_text.find(_DETAIL_SCRIPT_MARKER)
        detail = {}
        if idx != -1:
            val_start = idx + len(_DETAIL_SCRIPT_MARKER)
            val_end = html_text.find("')", val_start)
            if val_end != -1:
                blob = html_text[val_start:val_end]
                try:
                    decoded_json = _js_string_unescape(blob)
                    parsed = json.loads(decoded_json)
                    if isinstance(parsed, list) and parsed:
                        detail = parsed[0]
                except ValueError:
                    detail = {}
        _detail_cache[job_id] = detail

    description = _strip_html(detail.get("Job_Description") or "")
    posting_date = (detail.get("Date_Opened") or "").strip()

    return description, posting_date
