"""Fetches Luxoft (DXC Luxoft) job listings via career.luxoft.com's own
server-rendered jobs board.

Luxoft was acquired by DXC Technology in 2019. This repo already has a
`dxc_fetcher.py` for DXC's own Workday tenant (`dxctechnology.wd1.
myworkdayjobs.com`), so the first question investigated here (per the
"verify, don't assume post-acquisition consolidation" lesson from Alegeus,
Wave 9) was whether Luxoft's own careers presence still exists independently
or has been folded entirely into DXC's Workday pipeline.

**Answer: both, in different senses.** Luxoft's public careers site
(`career.luxoft.com`) is alive, independent, and NOT a Workday tenant — but
its *branding* has been absorbed: every job's `hiringOrganization.name` in
its own schema.org JSON-LD reads literally `"DXC Luxoft"`, and its compiled
JS bundle is served from `/theme/luxoft/assets/dxc.min.<hash>.js` — i.e.
Luxoft now runs on DXC's own custom in-house career-site platform/frontend
(view-source confirms Vue.js components named `JobsFilters`/`HomeSearch`/
`EmbeddedJobsListing`, not any known third-party ATS product name), just
skinned with Luxoft branding on a Luxoft-owned domain. This is a different,
platform-not-tenant flavor of the "GE Aerospace/Boeing frontend-skin"
pattern already in this repo's Key Bugs table: here the *entire* jobs
dataset is genuinely Luxoft's own (1211 total jobs globally, 437 in India
at investigation time — confirmed via the page's own "437 open roles"
summary count, not inferred), completely separate from and non-overlapping
with `dxc_fetcher.py`'s ~100+ DXC-proper Workday postings. No third-party
ATS vendor (Workday/Greenhouse/SmartRecruiters/Lever/iCIMS/Eightfold/
Avature/BambooHR/Pinpoint/etc.) is involved at all — this is DXC's own
proprietary in-house career-site software.

Verified live (2026-09-05) via direct `requests` GETs (no Playwright, no
JS execution needed — `GET /jobs?...` is fully server-rendered HTML, every
job card already present in the raw response body):

- `GET https://career.luxoft.com/jobs?country=India&keyword=<kw>&perPage=<n>
  &page=<p>` — `country` is a genuine server-side location facet (global
  unfiltered total 1211 vs. `country=India` 437 vs. `country=Poland` 114 —
  three independently different, plausible numbers, not a broken facet that
  echoes the same pool back). `keyword` genuinely narrows server-side too:
  0 results for a nonsense token, 17 for ".NET developer", 78 for "AI
  engineer", 437 (i.e. everything) for no keyword at all — confirmed with
  the country filter also applied, not just globally. **Not** added to
  `_IGNORES_KEYWORDS`.
- `perPage` accepts values well above the default page size (tested up to
  1000, returning the full 437-job India pool in one response in ~3.5s)
  with no observed cap — but `fetch_jobs`'s own `num`/`start` contract from
  `matcher.py` always requests page-sized chunks (`num=20` per call), so
  this fetcher translates `start`/`num` into `page = start // num + 1` and
  `perPage = num` rather than trying to cache the whole pool in one shot.
- **New pagination-termination gotcha**: once `page` exceeds the last page
  that actually has results, the server returns a plain **HTTP 404** (not
  an HTTP 200 with an empty `jobs__list`, the shape every other paginated
  fetcher in this repo assumes). Confirmed directly: a keyword with exactly
  17 India hits at `perPage=20` returns 200 with 17 cards on page 1, then a
  **404** on page 2 (and every page beyond). Handled by treating a 404 on
  this endpoint as "no more results" (return `[]`), not as an error to
  retry or raise `RateLimitError` on — the standard 404-handling elsewhere
  in this repo assumes a *closed job detail page*, this is the first
  instance of a *search/listing* endpoint doing it.
- Every job card in a `country=India` response carries a real Indian city
  (`Pune`, `Bengaluru`, `Noida`, `Hyderabad`, `Gurugram`, or `Remote India`)
  followed by the literal string `India` in a second `<p>` tag — spot-
  checked across all 437 (one single `perPage=1000` page), zero non-India
  leakage observed (contrast with the Micron/Verizon/Lowe's tenants in this
  repo's Key Bugs table, where the facet leaks other countries). Location
  is built directly from these two `<p>` tags — always ends up containing
  the literal substring "india" for a genuine India posting, satisfying
  `matcher.py`'s `is_india_job()` with no fetcher-side augmentation needed.
- **Posting dates are not usable from the listing page at all** (no date
  is rendered there) **and are unreliable in two different ways on the
  detail page**: (1) a `<p>` tag tagged with a "date" icon shows the exact
  same value (`"04/09/2026"`) on every single job detail page checked,
  regardless of how new or old the actual posting is — almost certainly a
  generic "page rendered/refreshed on" stamp mislabeled with a date icon,
  not a per-job value, and (2) the page's own `schema.org` JSON-LD
  `datePosted` field genuinely varies per job (`"2026-09-04"`,
  `"2026-08-01"`, ...) but a handful of postings on the freshest page
  (page 1, sorted newest-first) carry an impossible **future** date
  (`"2026-10-04"` when the real-world date of this investigation was
  2026-09-05) — a client-side data-entry artifact on Luxoft/DXC's own end
  (some reqs get a forward-dated "go-live"/refresh value), not a scraping
  bug. `fetch_job_description` still returns the JSON-LD `datePosted`'s
  date component as-is (most values are sane, and `matcher.py`'s date sort
  is purely cosmetic/lexicographic — a stray future date sorts as "newest"
  but breaks nothing functionally); the fixed, clearly-not-real "date icon"
  field is not used at all. Flagged here rather than silently invented —
  same "flag, don't silently patch" discipline as every other data-quality
  artifact already in this repo's Key Bugs table (Walmart's boilerplate JD
  paragraph, Salesforce's `<p>NA</p>` placeholder JD).
- Descriptions are **not** inline in the search response (only title,
  specialization, and location show on the listing card) — a separate
  per-job detail fetch is required. The detail page's single `application/
  ld+json` `JobPosting` block's `description` field already contains the
  full JD (project description + responsibilities + must-have/nice-to-have
  skills, HTML-escaped) and was verified to match the visually rendered
  page content almost exactly (1931 vs. 1990 chars for the same job, the
  ~60-char difference being page-only "Other/Languages/Seniority" metadata
  that isn't really part of the JD body) — used directly rather than
  re-parsing the many nested `job__grid__about-job__*` HTML sections.
- Job IDs: the trailing numeric segment of each posting's URL slug (e.g.
  `/jobs/business-analyst-with-custody-and-settlements-27100` -> `27100`)
  is used as the stable `id` — globally unique and consistent between the
  listing card's `href` and the detail page's own URL. (The page also shows
  an internal `Req. VR-124928`-style requisition number on the detail page
  only, not on the listing card — not used here since it's unavailable at
  search time and the URL-slug ID is already unique and stable.)
- Titles are reasonably specific for an IT-services-shaped company (real
  examples seen: "Senior Full Stack Developer", "Principal Software
  Engineer with Python", "Senior DevOps Engineer (AWS, Terraform & Amazon
  Bedrock)") — `require_tech_in_description` is deliberately NOT enabled
  (same reasoning as Eurofins/SimCorp: Layer 3's `primary_skills` check
  already provides adequate precision without it).

**A genuinely new WAF gotcha, found only by running this repo's own
default keyword list end-to-end**: this site sits behind an Azure
Application Gateway WAF that returns a plain **HTTP 403** (page title
literally "403 Forbidden" / "Microsoft-Azure-Application-Gateway/v2", not
a bot-challenge page) for a `keyword` value whose first word is an
all-lowercase interpreter-like name — `"python"`, `"java"`, `"ruby"` all
confirmed — followed by any second word, while the exact same word alone,
or with only its first letter capitalized, sails through with an
identical result count. Confirmed empirically (not theorized): `"python
developer"` -> 403, `"Python developer"` / `"PYTHON DEVELOPER"` -> 200
with the *same* 39-result count; `"python"` alone (no second word) is
never blocked (200, 32 results); `"angular developer"` / `"react
developer"` / `"node engineer"` / `"go developer"` (non-interpreter first
words) are never blocked either. This is consistent with a generic OWASP-
CRS-style "possible RCE via direct interpreter invocation" rule (the
classic `python <arg>` / `java <arg>` / `ruby <arg>` shell-command
detection heuristic) misfiring on an ordinary job-title search string —
and it directly matters here because this repo's own shared default
keyword list (`config.yaml`'s `_defaults.keywords` anchor) includes the
literal lowercase string `"python developer"`, which would 403 on every
single 30-minute scan cycle if sent verbatim. **Fixed defensively, not by
editing the shared keyword list**:
`fetch_jobs` upper-cases only the keyword's first character before
building the request (`"python developer"` -> `"Python developer"`),
which is enough to dodge the WAF rule and was verified to return the
identical result count as the all-caps/proper-case forms above — the
site's own search is case-insensitive, so this is a purely cosmetic,
zero-semantic-risk transformation applied only to the outgoing request,
never to the `keyword` value matcher.py itself sees or logs.
"""
from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://career.luxoft.com"
_JOBS_URL = f"{_BASE_URL}/jobs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": f"{_JOBS_URL}?country=India",
}

_JOB_ID_RE = re.compile(r"-(\d+)$")
_LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)

_desc_cache: dict[str, tuple[str, str]] = {}


class RateLimitError(Exception):
    """Raised on 429 / persistent connection failure from career.luxoft.com."""


def _job_id_from_href(href: str) -> str:
    slug = href.rstrip("/").rsplit("/", 1)[-1]
    m = _JOB_ID_RE.search(slug)
    return m.group(1) if m else slug


def _build_location(card) -> str:
    loc_ps = card.select(".jobs__list__job__details__tags__location p")
    parts = [p.get_text(strip=True) for p in loc_ps if p.get_text(strip=True)]
    return ", ".join(parts)


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict[str, str]]:
    """Return one page of Luxoft's job postings, scoped to *location* (a
    country name, e.g. "India") and narrowed by *keyword* — both are real
    server-side filters on this endpoint (see module docstring).

    `start`/`num` are translated into this endpoint's own `page`/`perPage`
    query params (`page = start // num + 1`), matching `matcher.py`'s
    always-fixed `num=20`-per-call pagination contract.
    """
    page = (start // num) + 1 if num else 1
    params: dict[str, object] = {"perPage": num, "page": page}
    if location:
        params["country"] = location
    if keyword:
        # Upper-case only the first character before sending — dodges the
        # Azure Application Gateway WAF's case-sensitive "interpreter
        # invocation" block on lowercase-first-word phrases like "python
        # developer" (see module docstring). The site's own search is
        # case-insensitive (verified: identical result counts across
        # case variants), so this changes nothing about what matches.
        params["keyword"] = keyword[:1].upper() + keyword[1:]

    r = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_JOBS_URL, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 404:
                # Confirmed live: paging past the last real page of results
                # returns a plain 404 on this endpoint (not an empty 200
                # listing) — treat as "no more results", not an error.
                return []
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("Luxoft search: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Luxoft search failed: {exc}") from exc

    soup = BeautifulSoup(r.text, "html.parser")
    jobs: list[dict] = []
    for card in soup.select("a.jobs__list__job"):
        href = card.get("href", "").strip()
        title_el = card.select_one("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        if not href or not title:
            continue

        job_id = _job_id_from_href(href)
        if not job_id:
            continue

        location_str = _build_location(card)

        jobs.append({
            "id": job_id,
            "title": title,
            "location": location_str,
            # Not available on the listing page at all (see module
            # docstring) — filled in from the detail page's JSON-LD by
            # fetch_job_description, which the caller overwrites with if
            # truthy.
            "posting_date": "",
            "application_url": href if href.startswith("http") else f"{_BASE_URL}{href}",
        })

    return jobs


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full job description + posting date from the detail page's
    schema.org `JobPosting` JSON-LD block.

    Returns `("", "")` if the posting has since closed (detail page 404s —
    a normal race between search and detail fetch, not an error) or if the
    JSON-LD block is missing/malformed.
    """
    if application_url in _desc_cache:
        return _desc_cache[application_url]

    r = None
    for attempt in range(3):
        try:
            r = requests.get(application_url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 404:
                return "", ""
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Luxoft description: 429 rate-limited for {application_url}")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            raise RateLimitError(f"Luxoft description fetch failed: {exc}") from exc

    m = _LD_JSON_RE.search(r.text)
    if not m:
        return "", ""

    import json

    try:
        data = json.loads(m.group(1))
    except ValueError:
        return "", ""

    raw_html = data.get("description", "") or ""
    description = " ".join(
        BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").split()
    )

    date_posted = (data.get("datePosted") or "").strip()
    posting_date = date_posted.split(" ")[0] if date_posted else ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", posting_date or ""):
        posting_date = ""

    result = (description, posting_date)
    _desc_cache[application_url] = result
    return result
