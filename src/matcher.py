"""Reads config, fetches live jobs, and applies location / title-family / skill filters."""

from __future__ import annotations

import html as html_mod
import re
import time
import warnings
from pathlib import Path
from typing import Any

import requests
import yaml

from fetcher import _HEADERS, RateLimitError, fetch_jobs

_DETAIL_BASE = "https://apply.careers.microsoft.com/api/apply/v2/jobs"

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _strip_html(raw: str) -> str:
    """Strip HTML tags and decode entities, returning a single whitespace-normalised string."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _normalize_text(text: str) -> str:
    """Normalise text so equivalent phrasings compare equal.

    Applied to both the job text and every search term before comparison, so
    whichever side uses an alias the match still fires.

    Transformations (in order):
      - Lowercase
      - ASP.NET → aspnet  (must precede the general .NET rule)
      - .NET (and variants) → dotnet
      - C# / C-Sharp / CSharp → csharp  (must precede hyphen normalisation)
      - Hyphens → spaces  (full-stack → full stack)
      - Roman numeral level suffixes: III → 3, II → 2  (whole word)
    """
    t = text.lower()
    # .NET family — ASP.NET first so it isn't partially consumed by the .NET rule
    t = t.replace("asp.net", "aspnet")
    t = re.sub(r"\.net\b", "dotnet", t)
    t = re.sub(r"\bdot\s+net\b", "dotnet", t)
    # C# family — before hyphen normalisation so "c-sharp" → "csharp" not "c sharp"
    t = re.sub(r"\bc#", "csharp", t)
    t = t.replace("c-sharp", "csharp")
    # Hyphens → spaces (covers full-stack, full-time, etc.)
    t = t.replace("-", " ")
    # Roman numeral level suffixes (whole word, already lowercased)
    t = re.sub(r"\biii\b", "3", t)
    t = re.sub(r"\bii\b", "2", t)
    return t


def _contains_any(text: str, terms: list[str]) -> bool:
    """Normalised substring check: does *text* contain at least one item from *terms*?"""
    normed = _normalize_text(text)
    return any(_normalize_text(t) in normed for t in terms)


# ---------------------------------------------------------------------------
# Individual filter predicates (pure functions, easy to unit-test)
# ---------------------------------------------------------------------------


def is_india_job(job: dict) -> bool:
    """True if the job's location string mentions India."""
    return "india" in job["location"].lower()


def passes_title_family_check(job: dict, title_family: list[str]) -> bool:
    """True if the job title belongs to the software-engineer family.

    Matching is fully normalised (case, hyphens, numeral variants, C#/.NET aliases).
    No specific seniority level is required — any level in the family passes.
    """
    return _contains_any(job["title"], title_family)


def passes_exclude_check(job: dict, exclude_terms: list[str]) -> bool:
    """True if the job title contains *none* of the configured exclude terms."""
    return not _contains_any(job["title"], exclude_terms)


def matches_skills(description: str, skills: list[str]) -> bool:
    """True if the plain-text description mentions at least one required skill."""
    return _contains_any(description, skills)


# ---------------------------------------------------------------------------
# Detail fetcher
# ---------------------------------------------------------------------------


def _ef_id_from_url(application_url: str) -> str:
    """Extract the numeric Eightfold job ID from the application URL.

    e.g. 'https://apply.careers.microsoft.com/careers/job/12345?domain=...' → '12345'
    """
    return application_url.split("/careers/job/")[1].split("?")[0]


def fetch_job_description(application_url: str, timeout: int = 20) -> str:
    """Fetch the full job description (plain text) for a single job.

    Uses the Eightfold detail endpoint; strips HTML before returning.
    """
    ef_id = _ef_id_from_url(application_url)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = requests.get(
            f"{_DETAIL_BASE}/{ef_id}",
            headers=_HEADERS,
            params={"domain": "microsoft.com"},
            timeout=timeout,
            verify=False,
        )
    r.raise_for_status()
    raw_html = r.json().get("job_description", "")
    return _strip_html(raw_html)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def find_matching_jobs(
    config_path_or_cfg,   # str | Path for existing callers; dict for new multi-board callers
    fetcher=None,         # None → use module-level MS functions; pass optum_fetcher for Optum
) -> tuple[int, list[dict]]:
    """Fetch all jobs from the API, apply every filter, and return results.

    Returns
    -------
    total_fetched : int
        Total unique jobs retrieved across all keyword/location combinations.
    matched : list[dict]
        Jobs that passed every filter.  Each dict has the standard five fields
        plus a ``description`` key with the plain-text job description.
    """
    cfg = (
        config_path_or_cfg
        if isinstance(config_path_or_cfg, dict)
        else load_config(config_path_or_cfg)
    )
    _fetch_jobs     = fetcher.fetch_jobs            if fetcher else fetch_jobs
    _fetch_desc     = fetcher.fetch_job_description if fetcher else fetch_job_description
    _RateLimitError = fetcher.RateLimitError        if fetcher else RateLimitError
    keywords: list[str] = cfg["search"]["keywords"]
    locations: list[str] = cfg["search"].get("locations", [])
    matching: dict = cfg.get("matching", {})
    skills: list[str] = matching.get("skills", [])
    title_family: list[str] = matching.get("title_family", [])
    exclude: list[str] = matching.get("exclude_terms", [])

    # --- Step 1: Fetch & deduplicate across all keyword × location combinations ---
    # Empty locations list means fetch without a location filter (date sort works).
    # A non-empty list drives one search pass per location value.
    _PAGE_SIZE = 20          # results requested per page
    _MAX_PAGES = 5           # at most 5 pages per keyword/location pair
    _INTER_PAGE_DELAY = 1.5  # seconds between consecutive search API calls

    seen_ids: set[str] = set()
    all_jobs: list[dict] = []
    for keyword in keywords:
        for location in (locations or [""]):
            start = 0
            for page_num in range(_MAX_PAGES):
                if page_num > 0:
                    time.sleep(_INTER_PAGE_DELAY)
                try:
                    page = _fetch_jobs(keyword, location, num=_PAGE_SIZE, start=start)
                except _RateLimitError as exc:
                    print(
                        f"  [warn] rate-limited for '{keyword}' / '{location}' "
                        f"after {page_num} page(s) — {exc}; skipping remaining pages"
                    )
                    break
                if not page:
                    break
                for job in page:
                    if job["id"] not in seen_ids:
                        seen_ids.add(job["id"])
                        all_jobs.append(job)
                start += len(page)

    total_fetched = len(all_jobs)

    # Sort newest-first; YYYY-MM-DD strings are lexicographically correct
    all_jobs.sort(key=lambda j: j["posting_date"], reverse=True)

    if all_jobs:
        print(
            f"Fetched {total_fetched} unique jobs — "
            f"newest: {all_jobs[0]['posting_date']}, "
            f"oldest: {all_jobs[-1]['posting_date']}"
        )

    # --- Step 2: Quick title/location filters (no extra HTTP calls) ---
    candidates: list[dict] = []
    filtered_out: list[str] = []
    for job in all_jobs:
        if not is_india_job(job):
            continue
        if not passes_exclude_check(job, exclude):
            filtered_out.append(f"[exclude]       {job['title']}")
            continue
        if not passes_title_family_check(job, title_family):
            filtered_out.append(f"[title family]  {job['title']}")
            continue
        candidates.append(job)

    # --- Step 3: Skill filter — fetch description only for remaining candidates ---
    # Each fetch gets one retry and a short inter-request delay to stay friendly
    # under the heavier load that pagination introduced.
    _TIMEOUT = 15       # seconds per attempt
    _RETRIES = 1        # one retry after the initial attempt
    _INTER_DELAY = 0.5  # seconds between consecutive detail fetches
    _RETRY_DELAY = 1.0  # seconds to wait before the retry attempt

    matched: list[dict] = []
    for i, job in enumerate(candidates):
        if i > 0:
            time.sleep(_INTER_DELAY)

        # Fetch with retry; stays None if all attempts fail.
        # optum_fetcher returns (description, posting_date); MS fetcher returns str.
        _fetch_result = None
        last_exc: Exception | None = None
        for attempt in range(1 + _RETRIES):
            try:
                _fetch_result = _fetch_desc(
                    job["application_url"], timeout=_TIMEOUT
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt < _RETRIES:
                    time.sleep(_RETRY_DELAY)

        if isinstance(_fetch_result, tuple):
            description, fetched_date = _fetch_result
            if fetched_date:
                job["posting_date"] = fetched_date
        else:
            description = _fetch_result

        if not description:
            # Fetch failed or returned empty — keep the job rather than risk
            # silently dropping a real role we can't verify.
            reason = f": {last_exc}" if last_exc else " (API returned empty body)"
            print(
                f"  [warn] description unavailable for '{job['title']}'"
                f"{reason} — keeping"
            )
            matched.append({**job, "description": ""})
            continue

        if matches_skills(description, skills):
            matched.append({**job, "description": description})
        else:
            # Detailed breakdown so near-misses are easy to diagnose.
            normed = _normalize_text(description)
            found = [s for s in skills if _normalize_text(s) in normed]
            filtered_out.append(
                f"[skill]         {job['title']} "
                f"(desc={len(description)} chars, "
                f"skills_found={found if found else 'none'})"
            )

    if filtered_out:
        print("India jobs filtered out (near-misses):")
        for line in filtered_out:
            print(f"  {line}")

    return total_fetched, matched
