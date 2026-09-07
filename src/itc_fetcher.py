r"""Fetches ITC Limited job listings via a Zoho Recruit "Career Site"
(recruitment.itcportal.com) -- the same self-hosted ATS product already
onboarded for Zoho Corporation itself (``zoho_fetcher.py``), a different
tenant here (not Zoho being its own client -- ITC genuinely runs its
public candidate-facing board on Zoho Recruit).

ATS discovery (live, 2026-09-07): itcportal.com/careers.html links to
``https://recruitment.itcportal.com/jobs/Careers`` -- confirmed live to be
the exact same Zoho Recruit Career Site product as Zoho's own tenant
(same ``zrc-`` CSS class prefixes, same embedded-JSON list mechanism).

Server-rendered: the full current job list is embedded directly in the
HTML as a JSON array inside a `<input type="hidden" id="jobs"
value="...">` tag, HTML-entity-escaped. No auth, no JS execution needed.
Confirmed live: exactly 52 currently-open postings (2026-09-07). Query
params (`keyword=`, `search=`) were tried against the list URL and never
change the embedded array (same finding as Zoho's own tenant -- reading
this product's shared `career-website-common.js` bundle confirms
keyword/location filtering happens entirely client-side in the browser
against the already-fully-loaded array) -- the whole board is cached once
per process, same "cache-once, ignores keywords" family as every other
Greenhouse/Lever/Ashby-style board in this repo.

**Unlike Zoho's own tenant, ITC's list payload is NOT truncated** --
confirmed live by comparing one job's list-page `Job_Description` (plain
text, 3532 chars) against the same job's detail-page `Job_Description`
(raw HTML, HTML-stripped down to the exact same 3532 chars) -- byte-for-
byte identical content, just pre-stripped-of-HTML on the list page. So no
separate per-job detail fetch is needed at all for the normal path;
`fetch_job_description` serves straight from the cache filled by
`fetch_jobs`, falling back to a live detail-page fetch only if called for
a job this process never cached (e.g. an isolated/unit-style call).

ITC's tenant also exposes real `City`/`State`/`Country` fields (Zoho's own
tenant only had a bare `Country1`) and a real `Date_Opened` field
("YYYY-MM-DD", already ISO) -- both used directly here, unlike Zoho's own
tenant where no posting-date field exists at all.

Country coverage (live, 2026-09-07): 51 of 52 postings carry `Country:
"India"` explicitly; the one exception ("Manager - Consumer Insights",
`City`/`Country` both null) has no location data at all. Given ITC's
public board is otherwise 100% India and this is a single unspecified-
location outlier (not a genuine overseas signal), it defaults to "India"
here -- same defensive-default convention as Zoho's own tenant's
``Country1 or "India"`` fallback -- rather than being dropped or left
blank (which would silently fail `is_india_job()` for a legitimately
India-based posting).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_LIST_URL = "https://recruitment.itcportal.com/jobs/Careers"
_DETAIL_URL_TMPL = "https://recruitment.itcportal.com/jobs/Careers/{job_id}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Module-level cache: the career-site page returns the identical full
# "Current Openings" list for every request (client-side-only filtering) --
# fetch it once and slice/filter after.
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False
_cache_error: "RateLimitError | None" = None


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
                raise RateLimitError(f"ITC {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ITC {what} failed: {exc}") from exc
    raise RateLimitError(f"ITC {what}: no response -- {last_exc}")


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _extract_hidden_input_json(html: str, input_id: str) -> list | dict | None:
    """Extract & decode a `<input ... value="[html-entity-escaped JSON]" id="{input_id}">`.

    Zoho's list page places `id="..."` immediately after the `value="..."`
    attribute it belongs to, so the search anchors on `id="{input_id}"` and
    walks backward to that value attribute's opening quote.
    """
    marker = f'id="{input_id}"'
    idx = html.find(marker)
    if idx < 0:
        return None
    val_start = html.rfind('value="', 0, idx)
    if val_start < 0:
        return None
    content_start = val_start + len('value="')
    content_end = html.find('"', content_start)
    if content_end < 0:
        return None
    raw = html[content_start:content_end]
    try:
        return json.loads(html_mod.unescape(raw))
    except (ValueError, TypeError):
        return None


def _decode_js_literal(raw: str) -> str:
    """Decode a JS single-quoted string body using Zoho's detail-page escape
    style (`\\x22` hex-escapes, `\\/`/`\\-` backslash-escaped literals,
    `\\\\` for backslash) -- NOT the same escaping as the list page."""
    def repl(m: re.Match) -> str:
        esc = m.group(1)
        if esc.startswith("x") and len(esc) == 3:
            try:
                return chr(int(esc[1:], 16))
            except ValueError:
                return esc
        if esc == "\\":
            return "\\"
        if esc == "n":
            return "\n"
        if esc == "t":
            return "\t"
        return esc  # e.g. \/ -> /, \- -> -

    return re.sub(r"\\(x[0-9a-fA-F]{2}|.)", repl, raw)


def _extract_detail_jobs(html: str) -> list | None:
    m = re.search(r"var jobs = JSON\.parse\('(.*?)'\);", html, re.S)
    if not m:
        return None
    try:
        return json.loads(_decode_js_literal(m.group(1)))
    except (ValueError, TypeError):
        return None


def _location_from_job(j: dict) -> str:
    parts = [
        (j.get(field) or "").strip()
        for field in ("City", "State", "Country")
        if (j.get(field) or "").strip()
    ]
    if not parts:
        return "India"
    loc = ", ".join(parts)
    country = (j.get("Country") or "").strip()
    if not country:
        # City/State present but Country itself is missing -- default
        # defensively to India (see module docstring: the only case
        # observed live is a fully-empty location, but this also covers a
        # partial one the same way) rather than silently failing
        # is_india_job() for what is, on this board, always an Indian
        # posting when country is merely omitted. A genuine non-India
        # Country value (e.g. "Oman") is left exactly as-is -- never
        # force-relabeled as India.
        loc = f"{loc}, India"
    return loc


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Zoho Career Site "Current Openings" list once and
    cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process. A failure is
    also remembered in _cache_error and re-raised on every subsequent call
    so this never silently degrades into a false "0 jobs" success.
    """
    global _cache_filled, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _cache_filled:
        return
    _cache_filled = True

    try:
        r = _get(_LIST_URL, timeout, "career-site list fetch")
    except RateLimitError as exc:
        _cache_error = exc
        raise

    raw_jobs = _extract_hidden_input_json(r.text, "jobs") or []

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
            "posting_date": (j.get("Date_Opened") or "").strip(),
            "application_url": _DETAIL_URL_TMPL.format(job_id=job_id),
        })
        full_desc = (j.get("Job_Description") or "").strip()
        if full_desc:
            _desc_cache[job_id] = _strip_html(full_desc)

    _job_cache[:] = collected
    print(f"[ITC] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of ITC Limited (Zoho Recruit Career Site) postings.

    keyword/location are accepted for interface compatibility but ignored --
    reading the page's own JS confirms search/location filtering happens
    entirely client-side against an already-fully-loaded job array; the
    shared matcher does the real title/skill/India filtering. The whole
    "Current Openings" list is cached once per process.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start: start + num]


def _job_id_from_url(application_url: str) -> str:
    return (application_url or "").rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single ITC job.

    Served from the cache filled by fetch_jobs (ITC's list payload already
    carries the full, non-truncated description -- see module docstring),
    falling back to a live detail-page fetch only for a job id this
    process hasn't cached yet.
    """
    job_id = _job_id_from_url(application_url)
    if not job_id:
        raise RateLimitError(
            f"ITC description: could not parse job id from {application_url!r}"
        )

    cached = _desc_cache.get(job_id)
    posting_date = ""
    for job in _job_cache:
        if job["id"] == job_id:
            posting_date = job["posting_date"]
            break
    if cached is not None:
        return cached, posting_date

    r = _get(
        _DETAIL_URL_TMPL.format(job_id=job_id), timeout, f"detail fetch {job_id}"
    )
    detail_jobs = _extract_detail_jobs(r.text) or []
    detail = detail_jobs[0] if detail_jobs else {}

    description = _strip_html(detail.get("Job_Description") or "")
    if description:
        _desc_cache[job_id] = description
    if not posting_date:
        posting_date = (detail.get("Date_Opened") or "").strip()

    return description, posting_date
