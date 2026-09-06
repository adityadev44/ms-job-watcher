r"""Fetches Zoho Corporation job listings via Zoho's own Zoho Recruit "Career
Site" product (self-hosted -- Zoho famously runs its own hiring pipeline on
its own SaaS products rather than a third-party ATS).

ATS discovery (live, 2026-09-06): zoho.com/careers/ links to
`careers.zohocorp.com/candidateportal` (the Zoho Recruit candidate login/
registration page -- confirmed by the `/recruit/viewCandidatePortalImage.do`
asset path in its HTML, i.e. genuinely served by the Zoho Recruit product).
The actual public job-listing page is the tenant's default "Careers" Zoho
Recruit Career Site page:

    GET https://careers.zohocorp.com/jobs/Careers

This is server-rendered: the full current job list is embedded directly in
the HTML as a JSON array inside a `<input type="hidden" id="jobs" value="...">`
tag, HTML-entity-escaped (`&#34;` for `"`). No auth, no JS execution needed to
read it. Verified live: exactly 2 currently-open postings system-wide
("Technical Support Engineers", "Sales Executives", both India) -- confirmed
genuine and not a fetcher bug by inspecting the embedded `pageJson` blob's own
`section.data[0].subtitle` label, which is literally "Current Openings"; this
is Zoho's complete self-service public board right now, same
"confirmed-low-volume-is-a-real-fact" precedent as PolicyBazaar/ING/eClerx
elsewhere in this repo -- Zoho evidently hires most engineers through campus
recruitment/internal channels rather than this public page. New postings
will flow through automatically the next time this pipeline runs.

Reading the page's own `career-website-common.js` bundle confirms keyword/
location filtering (the search box, the location facet) is applied ENTIRELY
client-side in the browser against the already-fully-loaded job array
(`searchJob()` does a plain JS `.forEach` + substring match over `group_record`,
no follow-up network request) -- there is no server-side query param at all,
so the whole board is fetched once per process and cached, same "cache-once,
ignores keywords" family as every other Greenhouse/Lever/Ashby-style board in
this repo.

The list page's embedded job objects are a truncated PREVIEW (`Job_Description`
ends in "...", no city/state, no posting-date field at all) -- ellipsis-free
descriptions are fetched from each job's own detail page:

    GET https://careers.zohocorp.com/jobs/Careers/{id}

which embeds the SAME job (full, non-truncated `Job_Description`, this time
real HTML) via a DIFFERENT encoding: an inline `var jobs = JSON.parse('...');`
JS statement using JS single-quoted-string escapes (`\x22` hex-escapes for
`"`, `\/`/`\-` backslash-escaped literals, `\\` for backslash) rather than the
list page's HTML-entity escaping -- `_decode_js_literal()` below undoes this
specific (and different-from-the-list-page) escaping style before `json.loads`.

No posting-date field is exposed anywhere in either payload (same "no
posting-date field" precedent as IBM/PolicyBazaar elsewhere in this repo) --
`posting_date` is left as `""` rather than fabricating one.

require_tech_in_description is NOT enabled -- current volume is too small (2
postings, neither an engineering role) for the extra filter to matter either
way; add it only if a future engineering posting turns out to need it.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_LIST_URL = "https://careers.zohocorp.com/jobs/Careers"
_DETAIL_URL_TMPL = "https://careers.zohocorp.com/jobs/Careers/{job_id}"

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
                raise RateLimitError(f"Zoho {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zoho {what} failed: {exc}") from exc
    raise RateLimitError(f"Zoho {what}: no response -- {last_exc}")


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


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the entire Zoho Career Site "Current Openings" list once and
    cache it.

    _cache_filled is set to True before the fetch attempt so a transient
    failure doesn't trigger a retry storm on every subsequent fetch_jobs()/
    fetch_job_description() call within the same process (Honeywell/
    Persistent lesson -- see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = _get(_LIST_URL, timeout, "career-site list fetch")
    raw_jobs = _extract_hidden_input_json(r.text, "jobs") or []

    collected: list[dict] = []
    for j in raw_jobs:
        job_id = str(j.get("id") or "").strip()
        title = (j.get("Posting_Title") or j.get("Job_Opening_Name") or "").strip()
        if not (job_id and title):
            continue
        location = (j.get("Country1") or "").strip() or "India"
        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",  # no posting-date field exposed anywhere
            "application_url": _DETAIL_URL_TMPL.format(job_id=job_id),
        })
        preview = (j.get("Job_Description") or "").strip()
        if preview:
            _desc_cache.setdefault(job_id, _strip_html(preview))

    _job_cache[:] = collected
    print(f"[Zoho] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Zoho Corporation (Zoho Recruit Career Site) postings.

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
    """Return (description, posting_date) for a single Zoho job.

    Fetches the job's own detail page for the full, non-truncated
    description (the list page's preview text ends in "..."). posting_date
    is always "" -- no such field is exposed anywhere on this career site.
    """
    job_id = _job_id_from_url(application_url)
    if not job_id:
        raise RateLimitError(
            f"Zoho description: could not parse job id from {application_url!r}"
        )

    r = _get(
        _DETAIL_URL_TMPL.format(job_id=job_id), timeout, f"detail fetch {job_id}"
    )
    detail_jobs = _extract_detail_jobs(r.text) or []
    detail = detail_jobs[0] if detail_jobs else {}

    description = _strip_html(detail.get("Job_Description") or "")
    if description:
        _desc_cache[job_id] = description
    else:
        description = _desc_cache.get(job_id, "")

    return description, ""
