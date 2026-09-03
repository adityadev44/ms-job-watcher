"""
Expedia Group India job fetcher — careers.expediagroup.com.

Prior secondary-research recon had flagged this as a "custom career site"
without ever opening DevTools. Live-verified from scratch (this is a real
GCC onboarding, not a re-use of a prior label — see PLAYBOOK.md's "Batch
Onboarding Wave 5" note on why that verification step matters): the public
job-search front end at ``careers.expediagroup.com`` is a WordPress site
running the third-party "Appcast Job Search" plugin
(``/wp-content/plugins/appcast-jobsearch-plugin/``, REST namespace
``wp-json/appcast-jobsearch/v1``). Search results are fully server-rendered
HTML — no JS execution needed, no Playwright. The *application* funnel
(Apply Now / Join Our Career Network / login links on every job page) goes
to a real Workday tenant (``expedia.wd108.myworkdayjobs.com`` /
``expedia.wd5.myworkdayjobs.com``), but that tenant's own search API is
never used here — Appcast's server-rendered HTML is the only thing this
fetcher talks to, same "candidate-facing skin sits in front of the real
ATS" shape as Boeing/TalentBrew (see PLAYBOOK.md Wave 5), just with a
different vendor and no Workday CXS call needed at all since descriptions
are also server-rendered.

Search page: ``GET /jobs/?filter[country]=India&keyword={keyword}`` returns
up to 25 results (``numpages`` embedded in the page as a JS var) directly in
the HTML as ``<li class="Results__list__item">`` entries — confirmed
server-side filtering for both ``filter[country]`` (a nonsense keyword with
``filter[country]=India`` correctly returns 0 of the usual India jobs) and
``keyword`` (e.g. "engineer" narrows 13 India jobs down to 9) via direct A/B
requests. India's current pool is tiny (13 jobs total across all keywords,
Bangalore + Gurgaon only) and always fits on page 1 (``numpages == "1"``),
so the ``/calc-results/?...&mypage=N`` "load more" pagination this module
also implements is defensive for future growth, not something exercised by
the pool as it stands today.

Job IDs are Expedia's own requisition IDs (``R-108740``); multi-location
postings suffix them (``R-104904``, ``R-104904-1``, ``R-104904-2`` for the
same req open in 3 different cities) so they stay unique per listing.

Each job-detail page (``/job/{slug}/{location-slug}/{id}/``) embeds a clean
schema.org ``JobPosting`` JSON-LD block with the full HTML description and
an authoritative ``datePosted`` — same pattern already used in this repo for
Societe Generale and Charles Schwab. No separate detail API call needed
beyond fetching that one HTML page.

Every current India posting is pure Python/cloud/AI-stack — zero mention of
.NET/C#/ASP.NET across all 13 live postings sampled during onboarding. Two
genuine ``AI / ML / Python`` matches exist today: "Software Development
Engineer II" (R-108740, Bangalore — explicit "generative ai" in the JD) and
"Machine Learning Engineer III, ML Operations" (R-109227, Bangalore —
explicit "generative ai" + "large language model"). A third strong
candidate, "Machine Learning Scientist III" (R-107837, "large language
model" in the JD), does NOT currently pass Layer 2 — "machine learning
scientist" isn't in the shared ``title_family`` list (only "machine
learning engineer" is) — same class of title-convention gap already flagged
for CitiusTech/PepsiCo in PLAYBOOK.md; not fixed here, since title_family is
a shared global list out of scope for a single-company onboarding.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://careers.expediagroup.com"
_SEARCH_URL = f"{_BASE_URL}/jobs/"
_CALC_RESULTS_URL = f"{_BASE_URL}/calc-results/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_ATTEMPTS = 3
_NUMPAGES_RE = re.compile(r'numpages\s*=\s*"(\d+)"')
_LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.S
)

# keyword -> parsed India job dicts. Confirmed the site's own keyword search
# genuinely narrows results server-side (see module docstring), so — unlike
# many companies in this repo — each distinct keyword value gets its own
# cache entry rather than being collapsed into a single ignores-keywords
# fetch. India's whole pool is tiny, so this stays cheap either way.
_keyword_cache: dict[str, list[dict]] = {}


class RateLimitError(Exception):
    """Raised when the site rate-limits (HTTP 429) or fails repeatedly."""


def _get(url: str, params: dict, timeout: int) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Request failed after {_MAX_ATTEMPTS} attempts"
            ) from exc

        if r.status_code == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")

        if r.status_code >= 500:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(
                f"Server error {r.status_code} after {_MAX_ATTEMPTS} attempts"
            )

        r.raise_for_status()
        return r

    raise RateLimitError(f"Rate-limited after {_MAX_ATTEMPTS} attempts")


def _parse_listing_items(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    for li in soup.find_all("li", class_="Results__list__item"):
        a = li.find("a", class_="view-job-button")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not job_id:
            continue
        title_el = li.find("h3", class_="Results__list__title")
        loc_el = li.find("h4", class_="Results__list__location")
        title = title_el.get_text(strip=True) if title_el else ""
        location = " ".join(loc_el.get_text(strip=True).split()) if loc_el else ""
        jobs.append({
            "id": job_id,
            "title": title,
            "location": location,
            # Not present on the listing page -- filled in from the detail
            # page's JSON-LD by fetch_job_description() (matcher.py applies
            # it back onto the job dict once fetched).
            "posting_date": "",
            "application_url": urljoin(_BASE_URL, href),
        })
    return jobs


def _fill_cache(keyword: str, timeout: int) -> list[dict]:
    """Fetch and cache every India job matching *keyword*, once per keyword.

    Sets the cache entry to ``[]`` before the first request completes (same
    discipline as this repo's other cache-once fetchers, e.g.
    persistent_fetcher.py) so a failure can't retry-storm across repeated
    calls in one scan cycle; a genuine failure on the very first page still
    propagates as RateLimitError so matcher.py can log and move on.
    """
    if keyword in _keyword_cache:
        return _keyword_cache[keyword]
    _keyword_cache[keyword] = []

    params: dict[str, str] = {"filter[country]": "India"}
    if keyword:
        params["keyword"] = keyword

    r = _get(_SEARCH_URL, params, timeout)

    seen_ids: set[str] = set()
    jobs: list[dict] = []
    for job in _parse_listing_items(r.text):
        if job["id"] not in seen_ids:
            seen_ids.add(job["id"])
            jobs.append(job)

    numpages_match = _NUMPAGES_RE.search(r.text)
    numpages = int(numpages_match.group(1)) if numpages_match else 1

    page = 2
    while page <= numpages:
        time.sleep(0.2)
        page_params = dict(params)
        page_params["mypage"] = str(page)
        try:
            r = _get(_CALC_RESULTS_URL, page_params, timeout)
        except RateLimitError:
            break  # keep whatever page 1 already gave us
        page_items = _parse_listing_items(r.text)
        if not page_items:
            break
        new_count = 0
        for job in page_items:
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                jobs.append(job)
                new_count += 1
        if new_count == 0:
            break  # defensive: avoid looping forever if pagination ever wraps
        page += 1

    _keyword_cache[keyword] = jobs
    return jobs


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return one page of Expedia India job listings matching *keyword*.

    *location* is accepted for interface compatibility but not used to build
    the request -- config.yaml only ever passes "India" for this company,
    and every request already hardcodes ``filter[country]=India`` server-side.
    """
    jobs = _fill_cache(keyword or "", timeout)
    return jobs[start:start + num]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch the full description and posting date for a single Expedia job.

    Returns (description, posting_date) where posting_date is 'YYYY-MM-DD',
    parsed from the detail page's schema.org JobPosting JSON-LD block (same
    pattern as societegenerale_fetcher.py / schwab_fetcher.py).
    """
    r = _get(application_url, {}, timeout)

    for match in _LD_JSON_RE.finditer(r.text):
        block = match.group(1)
        if "JobPosting" not in block:
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "JobPosting":
            continue
        raw_desc = data.get("description", "") or ""
        description = BeautifulSoup(raw_desc, "html.parser").get_text(
            separator=" ", strip=True
        )
        description = " ".join(description.split())
        posting_date = data.get("datePosted", "") or ""
        return description, posting_date

    return "", ""
