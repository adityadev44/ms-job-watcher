"""D. E. Shaw India job fetcher — custom Next.js SSR site (deshawindia.com).

The D. E. Shaw group (quant hedge fund) is a distinct company from
Arcesium (already tracked in this repo as ``arcesium`` — a D. E. Shaw
spinoff that has been an independent company since 2015 and is not touched
here). D. E. Shaw itself has a substantial India engineering/operations
presence run through its own dedicated India domain.

**Load-bearing discovery**: the main global site, ``deshaw.com/careers``,
is NOT where the India jobs live — its embedded Next.js
``__NEXT_DATA__.props.pageProps.regularJobs`` payload (84 postings) is
exclusively New York/Denver/London/Singapore, zero India, despite exposing
an "India" entry in its own location-filter facet list. D. E. Shaw runs a
**separate country-specific domain**, ``www.deshawindia.com``, discovered
via web search (not guessed) after the global site came back empty for
India. That domain's careers page
(``https://www.deshawindia.com/careers/work-with-us``) is built the same
way (Next.js, same underlying jobs schema) but its ``regularJobs`` payload
(84 postings as of 2026-09-08) is exclusively Hyderabad/Bengaluru/Gurugram
— i.e. this domain *is* the India-scoped pipeline, so no separate location
facet or filter logic is needed: every job returned by this page is
already India.

No public JSON API was found or needed — the entire job list, including
each posting's full description (``websiteDescription`` +
``responsibilities`` + ``peopleWeAreLookingFor`` bullet list), is
server-rendered directly into the page's ``__NEXT_DATA__`` script tag. Like
Arcesium/Clearwater/other small-pool companies in this repo, the whole
page is fetched once per process and cached — there is no per-keyword or
per-page query parameter this site respects (it is a single static SSR
page, not a search API), so this fetcher is registered in
``_IGNORES_KEYWORDS``.

No posting-date field exists anywhere in the schema (unlike Arcesium's
``updated_at``) — ``posting_date`` is always returned as ``""``.

Each posting can list multiple offices (``office: [{"name": "Hyderabad"},
...]``); all observed offices on this domain are India cities, so they are
joined and suffixed with ", India" to satisfy matcher.py's
``is_india_job()`` (which requires a literal "india" substring) without
guessing — no city-name allowlist is needed here (unlike Fidelity
International's tenant) because this domain has no non-India postings to
begin with.

Live data note (2026-09-08): confirmed real .NET/C#-free, Python/Go-heavy
India tech postings such as "Software Engineer (Linux)" (Python, Go,
Puppet, Prometheus), "Lead, Tech (Python Infra)", and several "GAITech" /
GenAI roles explicitly naming LangGraph and generative-AI production work
— a genuine AI/ML/Python-track employer, same shape as the research brief
expected for D. E. Shaw's Hyderabad software-development office.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time

import requests

_CAREERS_URL = "https://www.deshawindia.com/careers/work-with-us"
_JOB_BASE = "https://www.deshawindia.com/careers"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)

# Module-level cache: the full India job list is server-rendered into one
# page and fetched once per process (this site has no query-param-driven
# search API to page through).
_job_cache: list[dict] = []
_desc_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _build_description(job_description: dict) -> str:
    parts = []
    website_desc = job_description.get("websiteDescription")
    if website_desc:
        parts.append(website_desc)
    responsibilities = job_description.get("responsibilities")
    if responsibilities:
        parts.append(responsibilities)
    looking_for = job_description.get("peopleWeAreLookingFor")
    if isinstance(looking_for, list):
        parts.append(" ".join(str(x) for x in looking_for))
    elif looking_for:
        parts.append(str(looking_for))
    return _strip_html(" ".join(parts))


def _fill_cache(timeout: int = 20) -> None:
    """Fetch the D. E. Shaw India careers page once and cache its jobs.

    _cache_filled is set to True before the fetch attempt so a failure
    doesn't trigger a retry storm on every subsequent fetch_jobs() /
    fetch_job_description() call within the same process (Honeywell
    lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_CAREERS_URL, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("D. E. Shaw India: 429 rate-limited during cache fill")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"D. E. Shaw India cache fill failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"D. E. Shaw India cache fill: no response — {last_exc}")

    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        raise RateLimitError("D. E. Shaw India: __NEXT_DATA__ not found in page")

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise RateLimitError(f"D. E. Shaw India: __NEXT_DATA__ parse failed: {exc}") from exc

    raw_jobs = data.get("props", {}).get("pageProps", {}).get("regularJobs", [])
    collected: list[dict] = []
    for j in raw_jobs:
        d = j.get("data") or {}
        job_id = str(d.get("id") or "")
        title = (d.get("displayName") or "").strip()
        job_url = d.get("jobUrl") or ""
        if not (job_id and title and job_url):
            continue

        offices = [o.get("name", "") for o in (j.get("office") or []) if o.get("name")]
        loc = ", ".join(offices) if offices else "India"
        if "india" not in loc.lower():
            loc = f"{loc}, India"

        app_url = f"{_JOB_BASE}/{job_url}"

        _desc_cache[job_id] = _build_description(d.get("jobDescription") or {})

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc,
            "posting_date": "",
            "application_url": app_url,
        })

    _job_cache = collected
    print(f"[D. E. Shaw India] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of D. E. Shaw India jobs from the cached full list.

    keyword/location are accepted for interface compatibility but ignored:
    this is a static SSR page with no query-param search, and every job on
    this India-specific domain is already India-based.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single D. E. Shaw India job.

    Served entirely from the cache filled by _fill_cache() — the full
    description is already embedded in the SSR page's __NEXT_DATA__ blob.
    posting_date is always "" (no date field exists in this site's schema).
    """
    _fill_cache(timeout=timeout)

    job_id = application_url.rstrip("/").split("-")[-1]
    description = _desc_cache.get(job_id, "")

    return description, ""
