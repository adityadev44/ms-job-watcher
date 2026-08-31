"""
Zomato job fetcher — Darwinbox candidate portal, tenant "eternal".

ATS discovery (2026-08-31): www.zomato.com/careers HTTP-301-redirects to
www.eternal.com/careers (Zomato's parent company renamed itself "Eternal
Ltd" — Zomato/Blinkit/District/Hyperpure are all businesses under one
holding company now). That eternal.com/careers page is a fully
server-rendered Astro marketing page with ZERO job-search API calls —
confirmed both by grepping the raw HTML (no /api/, no greenhouse/lever/
smartrecruiters/darwinbox string) and by a live Playwright network capture
of every request the page fires while loading. It has no job listing at
all, just "Our businesses" blurbs and a bare `mailto:hr@eternal.com` link.

Two dead ends found while looking for the real ATS, both discovered from
the zomato.com CSP header, which still lists legacy vendor domains:
  - `*.recruiterbox.com` → zomato.recruiterbox.com redirects to
    zomato.hire.trakstar.com, which renders Trakstar Hire's own
    "Inactive account. This employer is no longer using Trakstar Hire to
    collect applications." page. Confirmed dead, not a transient issue.
  - SmartRecruiters board `careers.smartrecruiters.com/Zomato1` is real
    (`GET https://api.smartrecruiters.com/v1/companies/Zomato1/postings`
    returns valid JSON) but totalFound=3 for the company's entire
    history, releasedDate 2015-2016, and the one India posting's
    `jobAd.sections.companyDescription` text describes Zomato as an
    "online food discovery platform" active in "20 countries" — pre-IPO,
    pre-rebrand marketing copy from a decade ago. This board has not
    received a new posting in ~10 years; it is abandoned, not live.

The real, current ATS: Zomato/Eternal's HR system is Darwinbox, tenant
"eternal" (`eternal.darwinbox.in` — found by checking whether "eternal"
resolves as a genuine Darwinbox tenant subdomain; it does, unlike guesses
like "zomatomedia"/"district"/"hyperpure" which 500 with "Invalid
subdomain"). This domain's primary purpose is the internal employee HR
login portal, but Darwinbox bundles a public, unauthenticated external
candidate module at the same domain:

    GET https://eternal.darwinbox.in/ms/candidate/careers
        → the candidate-facing Angular SPA (no login required)
    GET https://eternal.darwinbox.in/ms/candidateapi/job?page=N&limit=M
        → JSON job search results (this is the exact call the SPA itself
          fires on load — confirmed via a real Firefox/Playwright network
          capture of https://eternal.darwinbox.in/ms/candidate/careers,
          not just a guessed endpoint)
    GET https://eternal.darwinbox.in/ms/candidateapi/job/{id}
        → JSON job detail (full HTML "jd" field)
    GET https://eternal.darwinbox.in/ms/candidateapi/job/filters
        → facet values (departments/locations/employment types)

No auth, no CSRF token, no session cookie needed — a cold, header-only
`requests.get()` returns the same JSON the live app gets. Endpoint names
(`job?page=`, `job/`, `job/filters`) were read directly out of the SPA's
webpack bundle (`main.*.js`, a `{...,jobList:"job?page=",
jobDetails:"job/",jobSearch:"job?",jobDropdownValues:"job/filters",...}`
route-name object), then verified live.

**Current live state — genuinely zero open postings, not a fetcher bug:**
`GET .../job?page=1` on the `eternal` tenant returns
`{"status":"success","message":{"jobscount":0,"jobs":[]}}`, and
`.../job/filters` returns every facet empty (`"departments":[]`,
`"functional_areas":[]`, etc. — only a placeholder "Remote" location).
This was independently reproduced by driving a real headless Firefox to
`/ms/candidate/careers` and capturing the exact XHR the app itself makes
on load — same empty result. Eternal's own `companyinfo` response
(`"company_name":"Eternal","recruitment_enabled":true,"new_careers":false`)
confirms the tenant is live and recruiting is switched ON; it simply has
no postings advertised through this channel right now. `new_careers:false`
means this tenant is pinned to Darwinbox's OLDER "candidate" front-end
(as opposed to the newer "candidatev2" SPA most active Darwinbox
customers now use) — every other `new_careers:false` tenant found during
this investigation (e.g. Ola Cabs) was *also* sitting at 0 jobs, which is
weak but consistent evidence this legacy front-end tier is barely used
for live hiring anymore, not merely an Eternal-specific gap.

**Why this fetcher can still be trusted despite Eternal's own board being
empty:** the JSON field shapes below (`title`, `officelocation_show_arr`,
`tool_tip_locations`, `job_posting_on`, `jd`, pagination via empty-`jobs`
termination, keyword params ignored server-side) were verified against
OTHER live, populated Darwinbox tenants running the exact same product —
`zepto.darwinbox.in` (41 jobs) and `hetero.darwinbox.in` (69 jobs) — since
Darwinbox's candidate API is identical across every customer; only the
data differs. `search=`/`keyword=`/`q=` params were tried against Zepto's
populated board and made no difference to the result set (server-side
ignored, matching the "ignores keywords" idiom used elsewhere in this repo
for Greenhouse/Lever-style boards) — so, like Razorpay/CRED/Groww, this
fetcher paginates through the *entire* board once per process and caches
it, letting the shared matcher do keyword/skill filtering client-side.
A `location=` param DOES change the result set (passing a free-text city
name collapsed 41 jobs to 0 on Zepto) — it likely expects a numeric
Darwinbox location ID we have no lookup for, so it is deliberately never
sent; sending it wrong would silently zero out real results.

**One real gap, called out rather than papered over:** `application_url`
below is a *best-effort construction*
(`https://eternal.darwinbox.in/ms/candidate/careers/job/{id}`), inferred
from the API's own literal route-name map (`jobDetails:"job/"` mirrors
every other route in that map 1:1 against its UI page, e.g.
`jobDropdownValues:"job/filters"` → the filters page). It is **not**
click-verified against a real rendered job page, because there are
currently zero live postings anywhere on this tenant to click through,
and no other `new_careers:false` tenant found had jobs either. This
should be re-checked (open a real alert link once Eternal posts its
first job under this pipeline) the same way Infosys/TCS URL formats were
originally guessed then confirmed in this repo's history — see
PLAYBOOK "Key Bugs".

Description quirk: the `jd` field is HTML-entity-escaped one extra level,
like Razorpay/Groww's Greenhouse boards (raw text starts
`&lt;p&gt;&lt;b&gt;...`, not `<p><b>...`) — `_strip_html` unescapes,
strips tags, then unescapes again for inline entities, same idiom as
`razorpay_fetcher.py`.

Live-verified 2026-08-31: the API calls above all return valid, correctly
-shaped JSON with zero errors/timeouts; `fetch_jobs("engineer", "India")`
returns `[]` (mechanically correct — the real upstream pool is empty
today), matching the "ING/eClerx: confirmed-zero-is-a-real-fact, not a
bug" precedent in this repo, just one level further upstream (zero at the
source, not zero after filtering). New Zomato/Eternal postings will be
picked up automatically the next time this pipeline runs, no code change
needed.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timezone

import requests

_TENANT = "eternal"
_BASE = f"https://{_TENANT}.darwinbox.in"
_API_BASE = f"{_BASE}/ms/candidateapi/"
_JOB_LIST_URL = f"{_API_BASE}job"
_JOB_DETAIL_BASE = f"{_API_BASE}job/"
_CAREERS_PAGE = f"{_BASE}/ms/candidate/careers"
# Best-effort candidate-facing URL — see module docstring "one real gap".
_JOB_PAGE_BASE = f"{_CAREERS_PAGE}/job/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
    "Referer": _CAREERS_PAGE,
}

_PAGE_SIZE = 50
_MAX_PAGES = 40  # safety cap (~2000 jobs) well beyond any plausible pool size

# Module-level cache: keyword/location params are ignored server-side
# (verified against a populated sibling Darwinbox tenant), so the whole
# board is paginated through once per process and sliced/looked-up after.
_job_cache: list[dict] = []
_detail_cache: dict[str, dict] = {}
_cache_filled: bool = False


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure."""


def _strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _epoch_to_date(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


def _location_from_job(job: dict) -> str:
    """Prefer the display location; fall back to the tooltip list for
    "Multiple locations" placeholders (same spirit as this repo's
    Barclays/Mastercard "N Locations" handling)."""
    loc = (job.get("officelocation_show_arr") or "").strip()
    if loc and loc.lower() != "multiple locations":
        return loc
    tips = [t.strip() for t in (job.get("tool_tip_locations") or []) if t and t.strip()]
    if tips:
        return "; ".join(tips)
    return loc or "India"


def _get_json(url: str, *, params: dict | None = None, timeout: int = 20, context: str = "") -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError(f"Zomato {context}: 429 rate-limited")
            r.raise_for_status()
            return r.json()
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"Zomato {context} failed after 3 attempts: {exc}") from exc
    raise RateLimitError(f"Zomato {context}: no response — {last_exc}")


def _fill_cache(timeout: int = 20) -> None:
    """Paginate through the entire Darwinbox board once and cache it.

    _cache_filled is set to True before the fetch loop starts so a
    transient failure doesn't retry-storm on every subsequent
    fetch_jobs()/fetch_job_description() call in the same process
    (Honeywell/CRED/Razorpay lesson — see PLAYBOOK "Key Bugs").
    """
    global _cache_filled
    if _cache_filled:
        return
    _cache_filled = True

    collected: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        data = _get_json(
            _JOB_LIST_URL,
            params={"page": page, "limit": _PAGE_SIZE},
            timeout=timeout,
            context=f"job list page {page}",
        )
        raw_jobs = ((data or {}).get("message") or {}).get("jobs") or []
        if not raw_jobs:
            break
        for j in raw_jobs:
            job_id = str(j.get("id") or "").strip()
            title = (j.get("title") or "").strip()
            if not (job_id and title):
                continue
            collected.append({
                "id": job_id,
                "title": title,
                "location": _location_from_job(j),
                "posting_date": _epoch_to_date(j.get("job_posting_on")),
                "application_url": f"{_JOB_PAGE_BASE}{job_id}",
            })

    _job_cache[:] = collected
    print(f"[Zomato] Cache filled: {len(collected)} total jobs")


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of Zomato (Darwinbox "eternal" tenant) postings.

    keyword/location are accepted for interface compatibility but ignored
    server-side (verified against a populated sibling Darwinbox tenant);
    the shared matcher does the real title/skill/India filtering. The
    whole board is paginated through and cached once per process.
    """
    _fill_cache(timeout=timeout)
    return _job_cache[start : start + num]


def _job_id_from_url(application_url: str) -> str:
    return application_url.rstrip("/").rsplit("/", 1)[-1]


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (description, posting_date) for a single Zomato job.

    Descriptions are NOT included in the list response — a separate
    detail call (`GET job/{id}`) is required and cached by job id.
    """
    job_id = _job_id_from_url(application_url)

    detail = _detail_cache.get(job_id)
    if detail is None:
        data = _get_json(
            f"{_JOB_DETAIL_BASE}{job_id}",
            timeout=timeout,
            context=f"job detail {job_id}",
        )
        job_list = ((data or {}).get("message") or {}).get("job") or []
        detail = job_list[0] if job_list else {}
        _detail_cache[job_id] = detail

    description = _strip_html(detail.get("jd") or "")
    posting_date = _epoch_to_date(detail.get("posted_on") or detail.get("job_posting_on"))
    return description, posting_date
