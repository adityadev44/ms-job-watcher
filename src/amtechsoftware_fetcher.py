"""Amtech Software India jobs via its employer-linked Greenhouse board."""
from __future__ import annotations

import html as _html_mod
import re
import time

import requests

_BOARD_TOKEN = "amtechsoftware"
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
    "Referer": "https://job-boards.greenhouse.io/amtechsoftware",
}

_india_cache: list[dict] = []
_content_cache: dict[str, str] = {}
_cache_filled: bool = False
_cache_error = None


class RateLimitError(Exception):
    """Raised on 429 / persistent network failure from Greenhouse."""


def _strip_html(raw: str) -> str:
    text = _html_mod.unescape(_html_mod.unescape(raw or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    # Portfolio marketing names Generative AI on every job, even Java/SRE
    # roles. Keep only the real job sections so it cannot satisfy skills.
    if text.lower().startswith('about vista equity partners'):
        boundary = re.search(r'\b(?:role description|position overview|job summary|schedule|key responsibilities)\b', text, re.I)
        text = text[boundary.start():] if boundary else ''
    return text


def _parse_date(job: dict) -> str:
    raw = job.get("first_published") or ""
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
                raise RateLimitError(f"Amtech Software {what}: 429 rate-limited")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Amtech Software {what} failed: {exc}") from exc
    raise RateLimitError(f"Amtech Software {what}: no response -- {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _india_cache, _cache_filled, _cache_error
    if _cache_filled:
        if _cache_error:
            raise _cache_error
        return
    _cache_filled = True

    try:
        r = _get_with_retry(f"{_LIST_URL}?content=true", timeout, "cache fill")
        raw_jobs = r.json().get("jobs", [])
    except (RateLimitError, ValueError) as exc:
        _cache_error = RateLimitError(f"Amtech Software cache failed: {exc}")
        raise _cache_error from exc

    collected: list[dict] = []
    for job in raw_jobs:
        job_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        if not (job_id and title):
            continue

        loc_name = ((job.get("location") or {}).get("name") or "").strip()
        if re.search(r"\b(?:bangalore|bengaluru)\b", loc_name, re.I) and not re.search(r"\bindia\b", loc_name, re.I):
            loc_name += ", India"
        if not re.search(r"\bindia\b", loc_name, re.I):
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
    print(f"[Amtech Software] Cache filled: {len(collected)} India jobs (of {len(raw_jobs)} total)")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    _fill_cache(timeout=timeout)
    return _india_cache[start : start + num]


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    _fill_cache(timeout=timeout)

    m = re.search(r"/jobs/(\d+)", application_url)
    job_id = m.group(1) if m else ""

    if job_id in _content_cache:
        for job in _india_cache:
            if job["id"] == job_id:
                return _strip_html(_content_cache[job_id]), job["posting_date"]

    if not job_id:
        return "", ""

    r = _get_with_retry(
        f"{_DETAIL_URL_TMPL.format(job_id=job_id)}?content=true", timeout, "detail fetch"
    )
    job = r.json()
    return _strip_html(job.get("content") or ""), _parse_date(job)
