"""Fetches Glean job listings via the Greenhouse ATS.

Glean's careers page (glean.com/careers) is Greenhouse-backed. The board
token is "gleanwork" (NOT "glean" -- that token 404s), discovered by probing
the public Greenhouse Job Board API directly:

    GET https://boards-api.greenhouse.io/v1/boards/gleanwork/jobs?content=true

Verified live 2026-08-31: HTTP 200, 109 total postings, 22 in India
(Bangalore + Mumbai).

Key quirks (same "cache-once, ignores keywords" family as every other
Greenhouse board in this repo -- Groww/Razorpay/AlphaSense):
- Greenhouse's job-list endpoint returns the ENTIRE current board in one
  call, no server-side pagination or keyword filtering. All jobs fetched
  once, cached in-module, `_cache_filled` set True *before* the fetch
  attempt (Honeywell/Persistent lesson re: retry storms).
- India filtering done inside the cache fill via case-insensitive "india"
  substring on `location.name` (Bangalore/Mumbai postings both say
  "Bangalore, India" / "Mumbai, India" directly -- no city-whitelist
  workaround needed here, unlike Ashby/Lever boards).
- `content` (full HTML) is DOUBLE HTML-entity-encoded -- e.g. the raw field
  starts `&lt;div class=&quot;content-intro&quot;&gt;...`, where the literal
  string itself contains an escaped "&amp;lt;div..." underneath. A SINGLE
  `html.unescape()` call only peels one layer, leaving visible "&nbsp;"
  entities and (for a genuinely double-escaped tag) unstripped "&lt;/lt;"
  fragments in the output; verified empirically against a real Glean posting
  that TWO successive unescape() calls are needed before tag-stripping to
  get clean text with no residual entities. This is the same double-encoding
  family flagged for Groww/Razorpay/AlphaSense, made explicit here since a
  single unescape was empirically insufficient (residual "&nbsp;" -> " "
  conversion requires the second pass).
- `first_published` (genuine original post date) preferred over `updated_at`
  (bumped on any edit) for posting_date, same as Groww/Razorpay.

Skill-match reality check (worth recording, no code implication): live
India titles are strong genuine engineering roles ("Software Engineer,
Agents", "Software Engineer, Agents Governance", "Software Engineer, Evals",
"Software Engineer, Machine Learning") with real agentic-AI/LLM-judge/RAG
subject matter in their description bodies -- but none of the sampled
descriptions use this repo's specific hard `primary_skills` terms (LangChain,
LangGraph, vector database, generative ai, etc.); they say "LLM" (a term this
repo deliberately excludes from primary_skills as a bare substring-collision
risk -- see PLAYBOOK "Scope Expansion") and otherwise describe agent/eval
systems in prose. So these are currently likely to be filtered out at Layer 3
under the existing global skill list -- a fetcher-external, config-level
observation, not a bug in this module. require_tech_in_description is NOT
enabled (titles are specific product-engineering titles, not generic bands,
and enabling it would only tighten an already-strict gate further).
"""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "gleanwork"
_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_LIST_URL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs"
_DETAIL_URL_TMPL = f"{_API_BASE}/{_BOARD_TOKEN}/jobs/{{job_id}}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://glean.com/careers",
}

_india_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Greenhouse."""


def _strip_html(raw: str) -> str:
    """Unescape twice (verified necessary for this board's double-encoded
    `content` field -- see module docstring), then strip tags."""
    text = _html_mod.unescape(_html_mod.unescape(raw or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_date(job: dict) -> str:
    raw = job.get("first_published") or job.get("updated_at") or ""
    return raw[:10] if raw else ""


def _get_with_retry(url: str, timeout: int, what: str) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Glean {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Glean {what} failed: {exc}") from exc
    raise RateLimitError(f"Glean {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _india_cache, _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    r = _get_with_retry(f"{_LIST_URL}?content=true", timeout, "cache fill")
    raw_jobs = r.json().get("jobs", [])

    collected: list[dict] = []
    for job in raw_jobs:
        job_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc_name = ((job.get("location") or {}).get("name") or "").strip()
        if "india" not in loc_name.lower():
            continue

        app_url = job.get("absolute_url") or f"https://job-boards.greenhouse.io/{_BOARD_TOKEN}/jobs/{job_id}"

        _content_cache[job_id] = job.get("content") or ""

        collected.append({
            "id": job_id,
            "title": title,
            "location": loc_name or "India",
            "posting_date": _parse_date(job),
            "application_url": app_url,
        })

    _india_cache = collected
    print(f"[Glean] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Glean India jobs.

    Greenhouse ignores keyword/location query params server-side and always
    returns the full current board, so the pool is fetched once and cached;
    keyword/location arguments are accepted for interface compatibility but
    not applied here (the shared title/skill filters in matcher.py do the
    real narrowing).
    """
    _fill_cache(timeout=timeout)
    return _india_cache[start: start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description_text, posting_date) for a single Glean job."""
    m = re.search(r"/jobs/(\d+)", application_url or "")
    job_id = m.group(1) if m else ""

    if job_id and job_id in _content_cache:
        return _strip_html(_content_cache[job_id]), ""

    if not job_id:
        raise RateLimitError(f"Glean description: could not parse job id from {application_url!r}")

    r = _get_with_retry(_DETAIL_URL_TMPL.format(job_id=job_id) + "?content=true", timeout, "description fetch")
    job = r.json()
    description = _strip_html(job.get("content") or "")
    posting_date = _parse_date(job)
    _content_cache[job_id] = job.get("content") or ""
    return description, posting_date
