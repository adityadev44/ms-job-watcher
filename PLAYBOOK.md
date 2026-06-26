# Job Watcher Playbook

Reference for maintaining this project and adding new company pipelines.

---

## What This System Does

Monitors job postings from multiple companies every 30 minutes via GitHub Actions. Filters for India-based .NET/C# software engineering roles. Sends Telegram + email alerts only for jobs not seen before. Each company has its own fetcher, run script, seen-jobs file, and config section — all isolated from each other.

---

## Filter Layers

**3 layers apply to every company. Wells Fargo has a 4th (opt-in only).**

**Layer 1 — Location**
- Job location must contain "India"
- Must not be Chennai, Tamil Nadu, Pune, Chandigarh, or Kochi (configured per company via `exclude_locations`)

**Layer 2 — Title**
- Title must match the software engineer family (`matching.title_family` in config)
- Title must not match `matching.exclude_terms` (no interns, managers, hardware, etc.)

**Layer 3 — Skills**
- Description must contain at least one **primary** .NET/C# skill (`.NET`, `C#`, `ASP.NET`, `Web API`, `SQL Server`, `T-SQL`, `Entity Framework`, `dotnet`) — Azure/Angular/TypeScript alone do not pass

**Layer 4 — Tech in title (Wells Fargo only — do not add to new companies by default)**
- Job title must explicitly contain a .NET/C# tech term (`require_tech_in_title` in config)
- Added because WF's generic "Senior Software Engineer" titles are mostly Java/Python roles
- Activated only in `run_wellsfargo.py` as a post-filter — not in `matcher.py`

Deduplication: jobs already in `seen_jobs_<company>.json` are never re-alerted (all companies).

---

## Architecture

```
config.yaml                    ← all config (search params + shared matching rules)
src/
  matcher.py                   ← shared filter engine (title family → exclude → skills)
  notifier.py                  ← Telegram + email alerts
  main.py                      ← Microsoft pipeline (uses fetcher.py)
  <company>_fetcher.py         ← data source: fetch_jobs() + fetch_job_description()
  run_<company>.py             ← pipeline entry point
seen_jobs_<company>.json       ← deduplication state
.github/workflows/watcher.yml  ← runs all pipelines in parallel
```

**matcher.py expects every fetcher to export exactly three things:**
```python
class RateLimitError(Exception): ...
def fetch_jobs(keyword, location, *, num, start, sort_by, timeout) -> list[dict]: ...
def fetch_job_description(application_url, timeout) -> tuple[str, str]: ...
```

Each job dict must have: `id`, `title`, `location`, `posting_date`, `application_url`.

---

## Filter Pipeline (All Companies)

```
fetch_jobs()
    └─ is_india_job()                  skip non-India silently
    └─ exclude_locations check         skip Chennai/Pune/Tamil Nadu silently
    └─ passes_exclude_check()          [exclude] tag in near-miss log
    └─ passes_title_family_check()     [title family] tag in near-miss log
        └─ fetch_job_description()
            └─ primary_skills check    [broad-only] / [react-only] / [skill] tags
```

**Layer 4 (Wells Fargo only — opt-in, never added by default):**
```
    └─ require_tech_in_title check     [title-tech] tag in near-miss log
```

Implemented in `run_wellsfargo.py` as a post-filter after `find_matching_jobs`. Not in `matcher.py`, not shared, not active for any other company.

---

## Current Companies

| Company | ATS | Fetch method | Entry point | Notes |
|---|---|---|---|---|
| Microsoft | Eightfold | REST API (JSON) | `main.py` | Original pipeline |
| Optum | TalentBrew | HTML scraping | `run_optum.py` | |
| Amazon | Custom | REST API (JSON) | `run_amazon.py` | |
| Siemens | Custom | HTML scraping | `run_siemens.py` | Keywords ignored server-side; fetches all, dedupes |
| Honeywell | Oracle HCM CE | **Playwright/Firefox** | `run_honeywell.py` | Chromium blocked by Akamai; titles use "Engr" not "Engineer" |
| Wells Fargo | Workday | REST API (JSON) | `run_wellsfargo.py` | India WID hardcoded; title-tech 4th filter active |
| Dell | Custom | REST API (JSON) | `run_dell.py` | |
| Oracle | Oracle HCM CE | REST API (JSON) | `run_oracle.py` | |
| MetLife | Custom | REST API (JSON) | `run_metlife.py` | Empty keyword fetches all India jobs; title/skills filter handles rest |
| FIS | Custom | REST API (JSON) | `run_fis.py` | |
| Chubb | Oracle HCM CE | REST API (JSON) | `run_chubb.py` | Tenant: fa-ewgu-saasfaprod1.fa.ocs.oraclecloud.com, site CX_2001 |
| S&P Global | Workday | REST API (JSON) | `run_spglobal.py` | No India facet; fetches globally, filters client-side; state-name normalisation for India detection |
| WTW | Oracle HCM CE | REST API (JSON) | `run_wtw.py` | Tenant: eedu.fa.em3.oraclecloud.com, site CX_1003; India facet unreliable — fetches globally, filters client-side |
| Morningstar | Phenom People | Sitemap + HTML scraping | `run_morningstar.py` | `/widgets` API not accessible without browser JS; sitemap has 208 jobs; each page's JSON-LD has full description — all fetched once and cached in-module |
| S&P Global Careers | iCIMS | REST API (JSON) | `run_spglobal_careers.py` | Separate portal from Workday pipeline (`careers.spglobal.com/api/jobs`); full description included in search response — no detail fetch needed |
| Gallagher (AJG) | iCIMS | REST API (JSON) | `run_gallagher.py` | `jobs.ajg.com/api/jobs`; identical iCIMS pattern to S&P Global Careers; India jobs in Kochi |
| Icertis | Oracle HCM CE | REST API (JSON) | `run_icertis.py` | Tenant: iaaviz.fa.ocs.oraclecloud.com, site Jobs-at-Icertis; no India facet — fetches globally, filters client-side; all current India jobs are in Pune (excluded) so 0 matches expected until Icertis opens non-Pune roles |
| Maersk | Workday | REST API (JSON) | `run_maersk.py` | `maersk.wd3.myworkdayjobs.com`; India location WIDs embedded as constant; all 122 India jobs fetched and cached once; `careers.maersk.com` API skipped (requires Consumer-Key and only returns 150 non-tech India jobs) |
| Nomura | SAP SuccessFactors J2W | HTML scraping | `run_nomura.py` | `careers.nomura.com/Nomura/go/Career-Opportunities-India/9050900/`; 337 India jobs; pagination via path segments (`/9050900/100/`, `/9050900/200/`), NOT `?startRow=N`; location format "Mumbai, IN" normalised to "Mumbai, India"; mostly Java/Python roles — .NET matches rare |
| American Express | Oracle HCM CE | REST API (JSON) | `run_amex.py` | Tenant: egug.fa.us2.oraclecloud.com, site CX_1; India location facet applied server-side |
| Fidelity | Workday | REST API (JSON) | `run_fidelity.py` | `fmr.wd1.myworkdayjobs.com`; India `locationCountry` facet; appends ", India" when missing from locationsText (city-only strings like "Bangalore, Karnataka") |
| Fiserv | Workday | REST API (JSON) | `run_fiserv.py` | `fiserv.wd5.myworkdayjobs.com`; no country facet — fetches globally, filters client-side |
| Goldman Sachs | Oracle HCM CE | REST API (JSON) | `run_goldmansachs.py` | Tenant: hdpc.fa.us2.oraclecloud.com, site LateralHiring; application_url points to public higher.gs.com (GraphQL BFF requires Okta auth; raw Oracle tenant is open) |
| JPMorgan Chase | Oracle HCM CE | REST API (JSON) | `run_jpmorgan.py` | Tenant: jpmc.fa.oraclecloud.com, site CX_1001; India location facet ID 300000000289360 |
| Marsh McLennan | Workday | REST API (JSON) | `run_marshmclennan.py` | `mmc.wd1.myworkdayjobs.com`; `Location_Country` facet (capitalised key, unlike other Workday tenants); appends ", India" when missing from locationsText |
| Mastercard | Workday | REST API (JSON) | `run_mastercard.py` | `mastercard.wd1.myworkdayjobs.com`; no country facet — uses "locations" facet with 8 India city WIDs; appends ", India" for "2 Locations" entries |
| Morgan Stanley | Eightfold | REST API (JSON) | `run_morganstanley.py` | `morganstanley.eightfold.ai`; same Eightfold PCSX API shape as Microsoft pipeline |
| Nagarro | SmartRecruiters | REST API (JSON) | `run_nagarro.py` | `careers.smartrecruiters.com/nagarro1`; `country=in` param filters server-side; keyword param is a loose pre-filter only (titles still need matcher's title-family check) |
| Citi | Workday | REST API (JSON) | `run_citi.py` | Tenant: `citi.wd5.myworkdayjobs.com`, site `2`; `Country_and_Jurisdiction` facet (not `locationCountry`); India WID `c4f78be1a8f14da0ab49ce1162348a5e`; ~339 India SW engineer jobs; "2 Locations" entries appended ", India" client-side |
| BNY Mellon | Oracle HCM CE | REST API (JSON) | `run_bny.py` | Tenant: `eofe.fa.us2.oraclecloud.com`, site `BNY-Careers`; India location facet ID `300000000378365`; majority of India jobs are Pune (excluded) — 0 matches expected until non-Pune .NET roles open |
| Northern Trust | Workday | REST API (JSON) | `run_northerntrust.py` | Tenant: `ntrs.wd1.myworkdayjobs.com`, site `northerntrust`; `locationCountry` WID `c4f78be1a8f14da0ab49ce1162348a5e` (same cross-tenant India GUID as Fidelity/Wells Fargo); page size capped at 20 |
| Deutsche Bank | Beesite + Workday | Beesite REST API (JSON) + Workday CXS descriptions | `run_deutsche.py` | Keywords and country filter ignored server-side; fetches all ~1808 global jobs, filters India (`CountryCode==IN`) client-side; descriptions via Workday CXS at `db.wd3.myworkdayjobs.com` |
| Barclays | Workday | REST API (JSON) | `run_barclays.py` | Tenant: `barclays.wd3.myworkdayjobs.com`; no `locationCountry` facet — uses 11 India city WIDs in `appliedFacets.locations`; `locationsText` omits "India" — appended client-side |
| UBS | IBM BrassRing | REST API (JSON) | `run_ubs.py` | `jobs.ubs.com`; CSRF token (`RFT` header) required per session — GET page first; descriptions inline in search results; ~15 India jobs; pagination wraps around — stop when no new IDs |
| Accenture | Workday (wd103) | REST API (JSON) | `run_accenture.py` | Tenant: `accenture.wd103.myworkdayjobs.com/AccentureCareers`; India WID `c4f78be1a8f14da0ab49ce1162348a5e`; ~24,731 India jobs; API caps at 2000 per query; `require_tech_in_title` active — essential |
| Infosys | Custom (in-house) | REST API (JSON) | `run_infosys.py` | `intapgateway.infosysapps.com/careersci/`; `searchText` is a no-op — all ~1558 India jobs returned per call; descriptions bundled in list response (no detail fetches); `require_tech_in_title` active |
| Cognizant | Custom (Umbraco CMS) | RSS feed (XML) | `run_cognizant.py` | RSS at `careers.cognizant.com/global-en/jobs/xml/?rss=true` returns all ~2069 jobs with full descriptions — no per-job fetch needed; India filtered client-side via `<country>` field; `require_tech_in_title` active |
| Capgemini | SAP SuccessFactors J2W | HTML scraping | `run_capgemini.py` | `careers.capgemini.com`; same J2W platform as Nomura but `?startrow=N` query-string pagination (not path-based); `locationsearch=india` server-side; location "City, IN" normalised to "City, India"; `require_tech_in_title` active |
| Synchrony | Workday | REST API (JSON) | `run_synchrony.py` | `synchronyfinancial.wd5.myworkdayjobs.com`; no country facet — uses "locations" facet with 6 India WIDs (Hyderabad + 5 Remote IN regions) |

---

## How to Add a New Company

> **Every company is different.** The steps below capture what worked across 6 past integrations. They are a starting point, not a checklist. Each new ATS will have its own quirks — different API shapes, bot protection, date formats, title conventions, or pagination schemes. Read what the new system actually does before reaching for a copy-paste from an existing fetcher. The goal is always accurate job alerts; the playbook is there to save time, not to constrain good judgment.

### Step 1 — Identify the ATS (10–20 min)

Open the careers page in a browser, search for a role, open DevTools → Network tab, look at XHR/fetch calls. Ask:

- **Does `requests.get/post` return job data?** → REST API approach (fast, like Optum/Wells Fargo)
- **Are all API responses empty or 403?** → Playwright needed (like Honeywell)
- **Is Chromium blocked?** → Use Firefox (Honeywell lesson)

Common ATS vendors and what to expect:

| ATS | Signal | Approach |
|---|---|---|
| **Workday** | URL contains `wd1.myworkdayjobs.com` or apply button links there | `POST /wday/cxs/{code}/{tenant}/jobs` with JSON body; India WID from facets |
| **Phenom People** | URL contains `/en/sites/{Company}/jobs` | `POST /widgets` with `refNum` extracted from page HTML |
| **Oracle HCM CE** | URL contains `fa.ocs.oraclecloud.com` or `oraclecloud.com` | Playwright/Firefox — JS SPA, API blocked server-side |
| **iCIMS** | URL contains `icims.com` | REST API or HTML scraping |
| **Greenhouse** | URL contains `greenhouse.io` | Public REST API, well-documented |
| **Lever** | URL contains `lever.co` | Public REST API |
| **SAP SuccessFactors J2W** | URL contains `careers.<company>.com` with job list at `/go/...` and detail at `/job/.../{id}/`; "J2W" (Job-to-Work) branding | HTML scraping — `<tr class="data-row">` table rows; pagination via path `/go/.../{offset}/`; description in `<span class="jobdescription">`; date in `<meta itemprop="datePosted">` |
| **Taleo** | URL contains `taleo.net` | HTML scraping usually required |
| **Phenom People** | URL contains `phenompeople.com` CDN assets or `refNum` in page JS | `/widgets` API requires browser session — use sitemap.xml + JSON-LD scraping |

### Step 2 — Map the response structure

Find: job ID field, title field, location field, posting date field, application URL field. These vary per ATS — check the raw JSON/HTML before writing code.

Watch out for:
- Relative dates ("Posted 3 Days Ago") — need conversion to `YYYY-MM-DD`
- Abbreviated titles ("Engr" instead of "Engineer") — may need additions to `title_family` in config
- JavaScript-rendered descriptions — plain `requests` may return empty; need Playwright or a JSON detail API

### Step 3 — Create `src/<company>_fetcher.py`

Copy the closest existing fetcher as a starting point:
- REST JSON → copy `wellsfargo_fetcher.py`
- HTML scraping → copy `siemens_fetcher.py`
- Playwright needed → copy `honeywell_fetcher.py`

Must export:
```python
class RateLimitError(Exception): ...

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20) -> list[dict]:
    # Returns: [{"id": ..., "title": ..., "location": ..., "posting_date": ..., "application_url": ...}]

def fetch_job_description(application_url, timeout=20) -> tuple[str, str]:
    # Returns: (description_text, posting_date_string)
```

Always include:
- Retry loop (3 attempts, exponential backoff)
- `RateLimitError` on 429 or persistent failure — matcher.py catches this and logs a warning instead of crashing
- Browser-like `User-Agent` header

### Step 4 — Create `src/run_<company>.py`

Copy `run_siemens.py` exactly. Change 4 things:
```python
import siemens_fetcher     →  import <company>_fetcher
"siemens_search"           →  "<company>_search"
seen_jobs_siemens.json     →  seen_jobs_<company>.json
source="Siemens"           →  source="<Company Name>"
```

Add at the top:
```python
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

Wrap the `__main__` block in try/except so a crash doesn't break other pipelines running in parallel.

### Step 5 — Add to `config.yaml`

Add a new section before `notifications:`:
```yaml
<company>_search:
  max_listings: 200
  inter_page_delay: 0.2       # be polite; 0.1 for fast APIs
  keywords:
    - "software engineer"
    - "senior software engineer"
    - ".NET developer"
    - "C# developer"
    - "dot net"
    - "angular"
  locations:
    - "India"
  exclude_locations:
    - "Chennai"
    - "Tamil Nadu"
    - "Pune"
    - "Chandigarh"
  # DO NOT add require_tech_in_title unless explicitly asked
```

Check if the company's ATS uses server-side keyword filtering (Workday does) or ignores keywords (Siemens doesn't). If it ignores keywords, all keywords will return the same result set and deduplication will handle it.

### Step 6 — Update `.github/workflows/watcher.yml`

Two additions only:

**In the parallel run step** (add next `pidN`):
```yaml
python -u src/run_<company>.py & pid7=$!
...
wait $pid7 || fail=1
```

**In the save step:**
```bash
test -f seen_jobs_<company>.json && git add seen_jobs_<company>.json || true
```

If the company needs Playwright: Firefox is already installed and cached. No workflow changes needed for that.

### Step 7 — Create `seen_jobs_<company>.json`

```json
[]
```

Commit this file alongside everything else.

### Step 8 — Test locally, then push

```bash
py src/run_<company>.py
```

Verify:
- Non-zero jobs fetched with India locations
- Near-miss log shows `[title family]`, `[skill]`, `[broad-only]` tags firing correctly — no obvious false positives
- No Java/Python/cloud-native jobs slipping through as matches
- Pune/Kochi/Chandigarh/Chennai/Tamil Nadu not in any matched result's location
- Alert fires (or "not sent (no new matches)" if all already seen)

---

## Key Bugs We Hit (Don't Repeat These)

| Bug | Root cause | Fix |
|---|---|---|
| `[broad-only]` false positives | Azure/Angular appearing in non-.NET JDs | `primary_skills` hard filter — broad skills alone never pass |
| Honeywell fetching 0 jobs | Oracle HCM CE returns empty body to plain HTTP | Switched to Playwright/Firefox |
| Chromium `ERR_HTTP2_PROTOCOL_ERROR` on Honeywell | Akamai TLS fingerprinting blocks headless Chromium | Use Firefox — confirmed working |
| Job titles empty (`inner_text()` = `""`) on Honeywell | KnockoutJS renders title in sibling element, not inside `<a>` | Follow `aria-labelledby` → `getElementById` → `.job-tile__title` |
| `_cache_filled` retry storm (Honeywell) | Flag set after the try-block, so every keyword retried on failure | Set `_cache_filled = True` before the try |
| "Software Engr II" filtered as `[title family]` | Honeywell abbreviates "Engineer" as "Engr" | Added `"software engr"` to global `title_family` in config |
| Descriptions only 16 chars (Honeywell) | `[class*='description']` matched a tiny label div | Added `len(t) > 100` guard before accepting matched element |
| Playwright workflow cached Chromium, ran Firefox | Copy-paste oversight | Cache key and install commands must both say `firefox` |
| Wells Fargo `fetch_job_description` returning empty | Workday job pages are JS SPAs — plain HTML has no content | Use JSON detail API: `GET /wday/cxs/wf/WellsFargoJobs{externalPath}` |
| Wells Fargo `limit=0` returns HTTP 400 | Workday rejects zero-result requests | Hardcode India WID discovered from a real search with `limit=1+` |
| `postedOn: "Posted Yesterday"` not parsed | `_parse_posted_on` only handled "X Days Ago" | Added explicit `"yesterday"` case |
| 92 matched Wells Fargo jobs (too many) | Description fetch was broken → matcher kept all as fallback | Fixed description → re-ran → 26; added title-tech filter → 2 |
| WTW India location facet (`300000000346515`) not filtering | Oracle HCM CE at `eedu.fa.em3.oraclecloud.com` ignores `selectedLocationsFacet` — returns Philippines job with India facet applied | Fetch globally (no facet); `is_india_job()` filters client-side — WTW's total job count is small enough (~70 per keyword) that this is fine |
| Morningstar Phenom `/widgets` API always returns `{"status":"failure"}` | Phenom People at `careers.morningstar.com` requires browser-side JS session state (PLAY_SESSION JWT + CSRF token) that plain HTTP cannot replicate | Use sitemap.xml (208 URLs) + JSON-LD on each page; filter India via `addressCountry`; cache all India jobs + descriptions in-module so subsequent keyword calls are free |
| Maersk `locationsText = "2 Locations"` bypasses India check | Workday shows "2 Locations" when a job is available in multiple sites; `is_india_job()` in matcher.py checks for "india" in location text, so these jobs were silently skipped | In `_fill_cache`, set `loc_text = "India"` when "india" is not in the location text — safe because we already pre-filtered with India WIDs |
| Maersk `careers.maersk.com` API not usable | Requires `Consumer-Key` header (extracted from frontend JS `api-keys.DfSBqKQY.js`) and only returns 150 India jobs — all non-technical (CSM, Finance, Operations) — none are software engineering roles | Use Workday directly: `maersk.wd3.myworkdayjobs.com/wday/cxs/maersk/Maersk_Careers/jobs` with India location WIDs in `appliedFacets.locations` |
| Nomura `?startRow=N` doesn't paginate | SuccessFactors J2W India portal uses path-based pagination, not query-string. `?startRow=100` returns the same 100 jobs as `?startRow=0` | Use path segments: `/9050900/100/` for page 2, `/9050900/200/` for page 3. Correct URLs discovered from the `<a class="paginationItemFirst">` links in the HTML |
| Citi facet key is `Country_and_Jurisdiction` not `locationCountry` | Citi's Workday tenant uses a non-standard facet key — sending `locationCountry` is silently ignored | Use `Country_and_Jurisdiction` as the facet key; India WID `c4f78be1a8f14da0ab49ce1162348a5e` |
| BNY Mellon is Oracle HCM CE not Workday | `bnymellon.wd1.myworkdayjobs.com` returned HTTP 422 for all site/tenant combos | Use Oracle HCM CE at `eofe.fa.us2.oraclecloud.com`, site `BNY-Careers`; same REST pattern as Chubb/Amex |
| Deutsche Bank country/keyword filter ignored server-side | Beesite API (`api-deutschebank.beesite.de/search/`) accepts but silently ignores `PositionCountry` and keyword criteria — always returns the full global pool | Cache all ~1808 jobs on first call, filter India by `CountryCode==IN` client-side; descriptions fetched from Workday CXS at `db.wd3.myworkdayjobs.com` |
| Barclays appears to use TalentBrew but is actually Workday | `search.jobs.barclays` loads TalentBrew JS as a frontend skin; apply links go to `barclays.wd3.myworkdayjobs.com` | Probe the underlying XHR requests; use Workday CXS directly with 11 India city WIDs |
| UBS is IBM BrassRing not Workday | All Workday probes (`ubs.wd1/wd3/wd5.myworkdayjobs.com`) returned 422 | Use IBM BrassRing at `jobs.ubs.com/TgNewUI/Search/Ajax/PowerSearchJobs`; extract CSRF token (`__RequestVerificationToken`) from page HTML and pass as `RFT` header |
| UBS pagination wraps around | `TotalJobsCount` is always 0; incrementing `PageNumber` eventually cycles back to the first page | Stop pagination when a page yields zero new job IDs |
| Accenture `total` field is 0 on paginated requests | Workday `total` field returns 0 for offset > 0 even when jobs are returned | Use empty `jobPostings` array as the termination signal, not `total` |
| Infosys `additionalResponsibility` has encoding corruption | Unicode U+2022 bullet characters inserted between every character (`•K•n•o•w•l•e•d•g•e`) | Omit `additionalResponsibility` field; use `technicalRequirement`, `rolesResponsibilities`, and `preferredSkills` instead |
| Capgemini uses `?startrow=N` not path-based pagination | Unlike Nomura (same J2W platform), Capgemini uses query-string pagination | Use `?startrow=25` for page 2, `?startrow=50` for page 3 etc. (25 per page) |

---

## Config Reference

```yaml
matching:                         # shared across ALL companies
  title_family: [...]             # titles that pass (e.g. "software engineer")
  exclude_terms: [...]            # titles that always fail (managers, interns, etc.)
  skills: [...]                   # at least one must appear in description
  primary_skills: [...]           # at least one of THESE must appear (no Azure-only pass)

<company>_search:                 # per-company, fully isolated
  max_listings: 200
  inter_page_delay: 0.2
  keywords: [...]
  locations: [...]
  exclude_locations:              # ALWAYS include Chennai, Tamil Nadu, Pune, Chandigarh, Kochi
    - "Chennai"
    - "Tamil Nadu"
    - "Pune"
    - "Chandigarh"
    - "Kochi"
  require_tech_in_title: [...]    # OPTIONAL — Wells Fargo only, do not add by default
```

---

## GitHub Actions

- All 39 pipelines run in **parallel** (`& pid=$!` pattern with `wait $pid || fail=1`)
- Firefox Playwright is **cached** via `actions/cache@v4` on `~/.cache/ms-playwright`
- `seen_jobs_*.json` files are committed back after each run with `[skip ci]` to prevent re-triggering
- Workflow is triggered manually (`workflow_dispatch`) — the cron expression in the file is intentionally left as a placeholder
