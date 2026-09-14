"""
Gramener job fetcher — custom WordPress/Elementor careers pages (no
third-party ATS).

Gramener (data-science/analytics/GenAI consultancy, Hyderabad/Bengaluru-
centric delivery) does NOT use any of the ATS vendors already seen in this
repo. Its `gramener.com/careers/` page is a plain server-rendered
WordPress page: every open role is its own static page at
`gramener.com/careers/<slug>/`, and the listing page renders a job-card
grid directly in the initial HTML with no JS execution required —
confirmed live 2026-09-13 via plain `requests.get` (35 job-card `<div>`
blocks, each carrying `data-location`/`data-department` attributes used
by the page's own client-side filter widget, but the underlying HTML is
already fully populated server-side).

Each card block has the shape:
    <div class="col-md-4 ... mb-4 gallery-item-1"
         data-location="Hyderabad, Bangalore, Chennai"
         data-department="data_scientist">
      ...
      <h4 ...>Data Scientist (GenAI &amp; NLP)</h4>
      ...
      <a href="https://gramener.com/careers/data-scientist-genai-nlp/" ...>

Location quirk: `data-location` is a comma-joined list of India cities
for postings open in multiple offices (e.g. "Hyderabad, Bangalore,
Chennai, Noida, Mumbai") — Chennai is a real, active Gramener office and
is on this repo's global `exclude_locations` list. matcher.py's exclude
check is a plain substring test against the *whole* location string, so
naively keeping the full comma-joined string would silently drop a
genuinely-Bengaluru-available posting purely because Chennai also appears
in the same string (the exact Energy Exemplar bug documented in
PLAYBOOK.md's Wave 9 section). Fixed the same way: pick the first
segment that is NOT one of this repo's default excluded location tokens
and use only that as the job's location (with ", India" appended); if
every listed segment is excluded, the original joined string is kept
as-is so the job is correctly dropped entirely by the shared exclude
check.

No job ID field exists — the URL slug is used as a stable id (dedup-safe:
each real posting has a unique detail-page URL).

No listing-level posting date is available; `fetch_jobs` returns an empty
`posting_date` and `fetch_job_description` fills in the real date from
each detail page's `<meta property="article:published_time">` tag.

Description: each detail page is also plain server-rendered HTML (no JS
needed). The actual JD content lives in one Elementor `text-editor`
widget starting at `<h3 class="mb-4">` (role title + years-of-experience)
through the "Apply for this role" button block
(`<div class="xamin-btn-container`) — verified live to contain real,
specific AI/ML detail (PyTorch, TensorFlow, BERT, LangChain, GPT,
Mistral/Falcon/Llama 2, Prompt Engineering) and .NET-track-irrelevant
(Gramener is AI/data-science only; no .NET/C# roles were observed on this
board, consistent with the company's actual stack).

35 total postings observed live, all data-science/analytics/full-stack-
Python/GenAI titles — no generic IT-services level-banded titles, so
require_tech_in_description is NOT enabled.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import requests

_LISTING_URL = "https://gramener.com/careers/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# This repo's global default exclude_locations (config.yaml), lower-cased,
# used to pick a non-excluded city out of a comma-joined multi-city
# `data-location` string. Kept as a local copy since fetchers don't have
# access to config.yaml at fetch time.
_EXCLUDED_CITY_TOKENS = (
    "chennai", "tamil nadu", "pune", "chandigarh", "kochi", "kerala",
    "trivandrum", "lucknow", "nagpur", "madurai", "kolkata", "indore",
    "vadodara",
)

_CARD_RE = re.compile(
    r'<div class="col-md-4 [^"]*"\s+data-location="([^"]*)"\s+data-department="[^"]*">'
    r'.*?<h4[^>]*>([^<]*)</h4>'
    r'.*?<a href="(https://gramener\.com/careers/[^"/]+/)"',
    re.S,
)

_PUBLISHED_RE = re.compile(
    r'<meta property="article:published_time" content="([^"]+)"'
)

_job_cache: list[dict] = []
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _pick_location(data_location: str) -> str:
    """Pick a single, matcher-safe location string from a comma-joined
    multi-city `data-location` attribute (see module docstring)."""
    segments = [s.strip() for s in data_location.split(",") if s.strip()]
    if not segments:
        return "India"
    for seg in segments:
        if seg.lower() not in _EXCLUDED_CITY_TOKENS:
            return f"{seg}, India"
    # Every listed city is excluded — keep the joined string so the shared
    # exclude_locations check still correctly drops this job.
    return f"{data_location}, India"


def _get(url: str, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Gramener: 429 rate-limited on {url}")
            r.raise_for_status()
            return r
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Gramener fetch failed ({url}): {exc}") from exc
    raise RateLimitError(f"Gramener fetch: no response ({url}) — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    global _cache_filled, _job_cache
    if _cache_filled:
        return
    _cache_filled = True

    r = _get(_LISTING_URL, timeout=timeout)

    collected: list[dict] = []
    seen_urls: set[str] = set()
    for data_location, title_raw, url in _CARD_RE.findall(r.text):
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = html_mod.unescape(title_raw).strip()
        if not title:
            continue

        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        location = _pick_location(data_location)

        collected.append({
            "id": job_id,
            "title": title,
            "location": location,
            "posting_date": "",
            "application_url": url,
        })

    _job_cache = collected
    print(f"[Gramener] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Gramener jobs from the cached listing page.

    keyword/location are accepted for interface compatibility but
    ignored — the listing page has no server-side keyword/location
    filtering; the shared matcher handles that downstream.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch a single Gramener job's detail page for description + date."""
    r = _get(application_url, timeout=timeout)
    text = r.text

    m = _PUBLISHED_RE.search(text)
    posting_date = (m.group(1)[:10] if m else "")

    start_idx = text.find('<h3 class="mb-4">')
    end_idx = text.find('<div class="xamin-btn-container', start_idx)
    if start_idx == -1:
        return "", posting_date
    if end_idx == -1:
        end_idx = start_idx + 8000  # generous fallback window
    description = _strip_html(text[start_idx:end_idx])

    return description, posting_date
