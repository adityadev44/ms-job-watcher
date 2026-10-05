"""
N-able (India GCC) job fetcher — SmartRecruiters public API.

ATS recon (2026-10-05):
  `careers.smartrecruiters.com` is Cloudflare-protected and not
  curl-accessible. SmartRecruiters identity was confirmed via its public
  REST API — `api.smartrecruiters.com/v1/companies/N-able/postings` returns
  HTTP 200 with an empty `content` list (0 open jobs as of recon date).

  N-able announced their Bengaluru GCC in the week of 2026-09-26 (100+
  engineers planned; Senior/Staff Software Engineer roles mentioned). No
  India postings are live yet — the fetcher returns [] today but will pick
  up roles automatically when hiring opens.

Search: `GET https://api.smartrecruiters.com/v1/companies/N-able/postings`
  Params: `status=published`, `country=in`, `q=<keyword>`, `limit=100`,
  `offset=<start>`. SmartRecruiters `country=in` is reliable; `q` applies a
  loose server-side keyword pre-filter. Both are used so the downstream
  matcher operates on a pre-filtered set rather than the full global board.

Description: per-job detail at `/v1/companies/N-able/postings/{id}` returns
  the full description via `jobAd.sections` (same pattern as Bosch in this
  repo). A module-level dict caches results so repeat calls within a process
  run hit no extra network.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_COMPANY = "N-able"
_BASE = f"https://api.smartrecruiters.com/v1/companies/{_COMPANY}/postings"
_PUBLIC = f"https://jobs.smartrecruiters.com/{_COMPANY}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    pass


def _get(url: str, timeout: int, params: dict | None = None) -> requests.Response:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"N-able SmartRecruiters 429 for {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(
                    f"N-able SmartRecruiters fetch failed for {url}: {exc}"
                ) from exc
            time.sleep(2 ** attempt)
    raise RateLimitError(f"N-able SmartRecruiters: unreachable ({url})")


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return N-able India jobs from SmartRecruiters filtered by keyword.

    `location` is accepted for interface compatibility but ignored — the
    `country=in` param already restricts to India server-side.
    """
    params = {
        "status": "published",
        "country": "in",
        "q": keyword,
        "limit": min(num, 100),
        "offset": start,
    }
    data = _get(_BASE, timeout, params).json()
    out: list[dict] = []
    for x in data.get("content", []):
        jid = str(x.get("id") or "")
        title = (x.get("name") or "").strip()
        loc = x.get("location") or {}
        if not jid or not title:
            continue
        if (loc.get("country") or "").lower() != "in":
            continue
        place = (
            loc.get("fullLocation")
            or loc.get("city")
            or "India"
        ).strip()
        if "india" not in place.lower():
            place = f"{place}, India"
        out.append({
            "id": jid,
            "title": title,
            "location": place,
            "posting_date": (x.get("releasedDate") or "")[:10],
            "application_url": f"{_PUBLIC}/{jid}",
        })
    return out


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single N-able job."""
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    jid = application_url.rstrip("/").split("/")[-1].split("-")[0]
    try:
        x = _get(f"{_BASE}/{jid}", timeout).json()
    except RateLimitError:
        raise
    except Exception:
        result: tuple[str, str] = ("", "")
        _desc_cache[application_url] = result
        return result

    sections = (x.get("jobAd") or {}).get("sections") or {}
    body = " ".join(
        _strip_html((sections.get(k) or {}).get("text") or "")
        for k in ("jobDescription", "qualifications", "additionalInformation")
    )
    posting_date = (x.get("releasedDate") or "")[:10]
    result = (body, posting_date)
    _desc_cache[application_url] = result
    return result
