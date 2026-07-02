# Job Watcher Playbook

Reference for maintaining this project and adding new company pipelines.

---

## What This System Does

Monitors job postings from multiple companies every 30 minutes via GitHub Actions. Filters for India-based .NET/C# software engineering roles. Sends Telegram + email alerts only for jobs not seen before. Each company has its own fetcher, run script, seen-jobs file, and config section — all isolated from each other.

---

## Filter Layers

**3 layers apply to every company. Companies where the shared skill check is too broad add a 4th (opt-in only).**

**Layer 1 — Location**
- Job location must contain "India"
- Must not be Chennai, Tamil Nadu, Pune, Chandigarh, Kochi, Kerala, Trivandrum, Lucknow, Nagpur, or Madurai (configured per company via `exclude_locations`)

**Layer 2 — Title**
- Title must match the software engineer family (`matching.title_family` in config)
- Title must not match `matching.exclude_terms` (no interns, managers, hardware, etc.)

**Layer 3 — Skills**
- Description must contain at least one **primary** .NET/C# skill (`.NET`, `C#`, `ASP.NET`, `Web API`, `SQL Server`, `T-SQL`, `Entity Framework`, `dotnet`) — Azure/Angular/TypeScript alone do not pass

**Layer 4 — Tech in description (Wells Fargo, Accenture, Infosys, Cognizant, Capgemini, TCS, Wipro, HCLTech, DXC, Citi, State Street, First American, Adobe, Sabre, Autodesk — opt-in, do not add to new companies by default)**
- Description must explicitly contain a **narrow** .NET/C#/ASP.NET term (`require_tech_in_description` in config) — narrower than Layer 3's `primary_skills`, which also passes on SQL Server/EF/Web API alone
- Originally added for IT-services shops whose generic titles ("Senior Software Engineer", "Software Engineer L3") give no reliable tech signal, where Layer 3's broader skill list was letting non-.NET roles through (e.g. HCLTech: Cisco Unified Comms, ServiceNow, GCP, Azure-monitoring roles that happened to mention SQL Server/EF); since extended to direct employers (Citi, State Street, First American, Adobe, Sabre, Autodesk) as a general precision tightener wherever the broader skill list alone risks false positives
- Activated as a post-filter in each company's `run_<company>.py` — not in `matcher.py`
- **Retired: title-based matching (`require_tech_in_title`).** Every company that used it now uses description matching instead — title text turned out to be too sparse a signal at IT-services shops (many real .NET roles carry a generic level-banded title with the tech named only in the JD body)

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

**Layer 4 (opt-in, never added by default):**
```
    └─ require_tech_in_description check [desc-tech] tag in near-miss log
```
Implemented per-company as a post-filter after `find_matching_jobs` (`run_wellsfargo.py`, `run_accenture.py`, `run_infosys.py`, `run_cognizant.py`, `run_tcs.py`, `run_capgemini.py`, `run_wipro.py`, `run_hcltech.py`, `run_dxc.py`, `run_citi.py`, `run_statestreet.py`, `run_firstamerican.py`, `run_adobe.py`, `run_sabre.py`, `run_autodesk.py`). Not in `matcher.py`, not shared.

---

## Current Companies

| Company | ATS | Fetch method | Entry point | Notes |
|---|---|---|---|---|
| Microsoft | Eightfold | REST API (JSON) | `main.py` | Original pipeline |
| Optum | TalentBrew | HTML scraping | `run_optum.py` | |
| Amazon | Custom | REST API (JSON) | `run_amazon.py` | |
| Siemens | Custom | HTML scraping | `run_siemens.py` | Keywords ignored server-side; fetches all, dedupes |
| Honeywell | Oracle HCM CE | **Playwright/Firefox** | `run_honeywell.py` | Chromium blocked by Akamai; titles use "Engr" not "Engineer" |
| Wells Fargo | Workday | REST API (JSON) | `run_wellsfargo.py` | India WID hardcoded; `require_tech_in_description` 4th filter active |
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
| Citi | Workday | REST API (JSON) | `run_citi.py` | Tenant: `citi.wd5.myworkdayjobs.com`, site `2`; `Country_and_Jurisdiction` facet (not `locationCountry`); India WID `c4f78be1a8f14da0ab49ce1162348a5e`; ~339 India SW engineer jobs; "2 Locations" entries appended ", India" client-side; `require_tech_in_description` active |
| BNY Mellon | Oracle HCM CE | REST API (JSON) | `run_bny.py` | Tenant: `eofe.fa.us2.oraclecloud.com`, site `BNY-Careers`; India location facet ID `300000000378365`; majority of India jobs are Pune (excluded) — 0 matches expected until non-Pune .NET roles open |
| Northern Trust | Workday | REST API (JSON) | `run_northerntrust.py` | Tenant: `ntrs.wd1.myworkdayjobs.com`, site `northerntrust`; `locationCountry` WID `c4f78be1a8f14da0ab49ce1162348a5e` (same cross-tenant India GUID as Fidelity/Wells Fargo); page size capped at 20 |
| Deutsche Bank | Beesite + Workday | Beesite REST API (JSON) + Workday CXS descriptions | `run_deutsche.py` | Keywords and country filter ignored server-side; fetches all ~1808 global jobs, filters India (`CountryCode==IN`) client-side; descriptions via Workday CXS at `db.wd3.myworkdayjobs.com` |
| Barclays | Workday | REST API (JSON) | `run_barclays.py` | Tenant: `barclays.wd3.myworkdayjobs.com`; no `locationCountry` facet — uses 11 India city WIDs in `appliedFacets.locations`; `locationsText` omits "India" — appended client-side |
| UBS | IBM BrassRing | REST API (JSON) | `run_ubs.py` | `jobs.ubs.com`; CSRF token (`RFT` header) required per session — GET page first; descriptions inline in search results; ~15 India jobs; pagination wraps around — stop when no new IDs |
| Accenture | Workday (wd103) | REST API (JSON) | `run_accenture.py` | Tenant: `accenture.wd103.myworkdayjobs.com/AccentureCareers`; India WID `c4f78be1a8f14da0ab49ce1162348a5e`; ~24,731 India jobs; API caps at 2000 per query; `require_tech_in_description` active — essential |
| Infosys | Custom (in-house) | REST API (JSON) | `run_infosys.py` | `intapgateway.infosysapps.com/careersci/`; `searchText` is a no-op — all ~1558 India jobs returned per call; descriptions bundled in list response (no detail fetches); `require_tech_in_description` active |
| Cognizant | Custom (Umbraco CMS) | RSS feed (XML) | `run_cognizant.py` | RSS at `careers.cognizant.com/global-en/jobs/xml/?rss=true` returns all ~2069 jobs with full descriptions — no per-job fetch needed; India filtered client-side via `<country>` field; `require_tech_in_description` active |
| Capgemini | SAP SuccessFactors J2W | HTML scraping | `run_capgemini.py` | `careers.capgemini.com`; same J2W platform as Nomura but `?startrow=N` query-string pagination (not path-based); `locationsearch=india` server-side; location "City, IN" normalised to "City, India"; `require_tech_in_description` active |
| TCS | iBegin (proprietary) | REST API (JSON) | `run_tcs.py` | `ibegin.tcsapps.com`; POST `/candidate/api/v1/jobs/searchJ`; India-only portal; 10 jobs/page (fixed); keyword `#` breaks search — `"C#"` matches all 4,227 India jobs; use `"csharp"` or `"dotnet"` instead; apply-by date used as posting date proxy; `require_tech_in_description` active |
| Synchrony | Workday | REST API (JSON) | `run_synchrony.py` | `synchronyfinancial.wd5.myworkdayjobs.com`; no country facet — uses "locations" facet with 6 India WIDs (Hyderabad + 5 Remote IN regions) |
| LSEG | Workday | REST API (JSON) | `run_lseg.py` | `lseg.wd3.myworkdayjobs.com`, site `careers`; `locationCountry` facet; large Bengaluru centre, frequent .NET roles; office-code locations ("IND-BLR-…") get ", India" appended |
| State Street | Workday | REST API (JSON) | `run_statestreet.py` | `statestreet.wd1.myworkdayjobs.com`, site `Global`; `Location_Country` facet (capitalised, like MMC); ~200 India jobs; Coimbatore excluded by name (location text omits "Tamil Nadu"); `require_tech_in_description` active |
| Broadridge | Workday | REST API (JSON) | `run_broadridge.py` | `broadridge.wd5.myworkdayjobs.com`, site `Careers`; `Location_Country` facet; .NET-heavy shop, ~30 India jobs (Bengaluru/Hyderabad) |
| Kyndryl | Workday | REST API (JSON) | `run_kyndryl.py` | `kyndryl.wd5.myworkdayjobs.com`, site `KyndrylProfessionalCareers`; `locationCountry` facet; ~290 India software jobs |
| DXC Technology | Workday | REST API (JSON) | `run_dxc.py` | `dxctechnology.wd1.myworkdayjobs.com`, site `DXCJobs`; `locationCountry` facet; locations use state codes ("IND - TN - CHENNAI") — "- TN -" added to exclude_locations to cover all Tamil Nadu cities; `require_tech_in_description` active — IT-services generic titles |
| Ameriprise | Workday | REST API (JSON) | `run_ameriprise.py` | `ameriprise.wd5.myworkdayjobs.com`, site `ameriprise`; `locationCountry` facet; Hyderabad/Noida/Gurugram |
| FactSet | Workday | REST API (JSON) | `run_factset.py` | `factset.wd108.myworkdayjobs.com`, site `FactSetCareers`; no country facet — global fetch (~60 jobs) + client-side India filter; .NET-heavy Hyderabad centre |
| PayPal | Workday | REST API (JSON) | `run_paypal.py` | `paypal.wd1.myworkdayjobs.com`, site `jobs`; no country facet — global fetch + word-boundary India filter (`\bindia\b`, so "Indianapolis" never passes); small India presence, 0 matches often expected |
| Invesco | Workday | REST API (JSON) | `run_invesco.py` | `invesco.wd1.myworkdayjobs.com`, site `IVZ`; no country facet AND locationsText omits "India" ("Hyderabad, Telangana") — India detected via city/state tokens; .NET-heavy Hyderabad centre |
| First American | Workday | REST API (JSON) | `run_firstamerican.py` | `firstam.wd1.myworkdayjobs.com`, site `faicareers` — First American India's dedicated portal, every posting is India (all Bangalore); heavily .NET shop; ", India" appended to "IND, Karnataka, Bangalore" locations; `require_tech_in_description` active |
| Standard Chartered | SAP SuccessFactors (Job2Web Unify) | REST API (JSON) | `run_standardchartered.py` | `jobs.standardchartered.com`, categoryId `9783657`; CSRF-token + session-cookie handshake; `facetFilters.jobLocationCountry` restricts to India (~280 jobs); keywords/location ignored server-side |
| Wipro | SAP SuccessFactors (Job2Web Unify) | REST API (JSON) | `run_wipro.py` | `careers.wipro.com`, categoryId `0`; same Unify pattern as Standard Chartered; ~3700 India jobs; `require_tech_in_description` active — see Key Bugs, was previously a dead `require_tech_in_title` config that `run_wipro.py` never read; generic "SOFTWARE ENGINEER L3/L4" titles dominate |
| HCLTech | SAP SuccessFactors (Job2Web Unify) | REST API (JSON) | `run_hcltech.py` | `careers.hcltech.com`, India-only categoryId `9553955`; same Unify pattern, different field names (`custprimecity`/`custCountryRegion` instead of `jobLocationShort`/`jobLocationCountry`); ~8000 India jobs; `require_tech_in_description` active — see Key Bugs, was previously a dead `require_tech_in_title` config |
| HSBC | Eightfold | REST API (JSON) | `run_hsbc.py` | `portal.careers.hsbc.com` (migrated off the old Avature `mycareer.hsbc.com` portal); public `pcsx/search` API disabled tenant-wide — uses the "related jobs" widget endpoint anchored to a hardcoded real job ID; hard-capped at 10 results, no pagination |
| MSCI | Algolia (direct) | REST API (JSON) | `run_msci.py` | `careers.msci.com` frontend queries Algolia directly (app `RVMOB42DFH`) with a public search-only API key; ~90 jobs total, ~18 India, single unfiltered query covers everything; full description embedded in each hit — no detail fetch needed |
| Target | Workday | REST API (JSON) | `run_target.py` | `target.wd5.myworkdayjobs.com`, site `targetcareers`; India GCC in Bangalore; capitalised `Location_Country` facet; ~58 India software-engineer jobs |
| Adobe | Workday | REST API (JSON) | `run_adobe.py` | `adobe.wd5.myworkdayjobs.com`, site `external_experienced`; India centres in Bangalore + Noida; `locationCountry` facet works despite not being listed in the tenant's advertised facets; `require_tech_in_description` active |
| Micron | Workday | REST API (JSON) | `run_micron.py` | `micron.wd1.myworkdayjobs.com`, site `External`; Hyderabad "Phoenix Aquila" campus; semiconductor/hardware — low .NET match volume expected; **`locationCountry` facet unreliable (~85% non-India leakage)** — fetcher does not append ", India", relies on `is_india_job()` to filter genuinely |
| Sabre | Workday | REST API (JSON) | `run_sabre.py` | `sabre.wd1.myworkdayjobs.com`, site `SabreJobs`; travel-technology (GDS) company, Bengaluru engineering centre; `require_tech_in_description` active |
| Autodesk | Workday | REST API (JSON) | `run_autodesk.py` | `autodesk.wd1.myworkdayjobs.com`, site `Ext`; India centres in Bengaluru + Pune; locationsText uses abbreviated "IND" country code; `require_tech_in_description` active |
| Verizon | Workday | REST API (JSON) | `run_verizon.py` | `verizon.wd12.myworkdayjobs.com`, site `verizon-careers`; Hyderabad network/telecom engineering; **`locationCountry` facet unreliable** (genuine US locations leak through) — fetcher does not append ", India" |
| Lowe's | Workday | REST API (JSON) | `run_lowes.py` | `lowes.wd5.myworkdayjobs.com`, site `LWS_External_CS`; Bengaluru engineering centre; **`locationCountry` facet unreliable** (US locations leak through) — fetcher appends ", India" only for recognised India city names, not blindly |
| eBay | Workday | REST API (JSON) | `run_ebay.py` | `ebay.wd5.myworkdayjobs.com`, site `apply`; Bengaluru engineering centre; capitalised `Location_Country` facet |
| General Motors | Workday | REST API (JSON) | `run_generalmotors.py` | `generalmotors.wd5.myworkdayjobs.com`, site `Careers_GM`; GM India Technical Centre in Bengaluru — small footprint (~5 total postings), 0 matches often expected; capitalised `Location_Country` facet |

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
| **SAP SuccessFactors J2W (classic)** | URL contains `careers.<company>.com` with job list at `/go/...` and detail at `/job/.../{id}/`; "J2W" (Job-to-Work) branding; view source shows `<tr class="data-row">` rows | HTML scraping — pagination via path `/go/.../{offset}/`; description in `<span class="jobdescription">`; date in `<meta itemprop="datePosted">` |
| **SAP SuccessFactors J2W (Unify theme)** | Same `/go/` or `/search/` URLs as classic J2W, but view source shows NO `data-row` rows — results are empty until JS runs. Scripts include `j2w.searchResultsUnify.min.js` | REST API — POST JSON to `/services/recruiting/v1/jobs` with `facetFilters`; needs a `x-csrf-token` header (scraped from a `var CSRFToken = "..."` in the page) + session cookie from a prior GET; `pageNumber` must be walked manually (ignores `start`/`offset`). Detail pages ARE server-rendered HTML — extract by anchoring on the "Job Description:" label, not `itemprop="description"` (that attribute can double up on an unrelated company blurb) |
| **Algolia (direct)** | Page's JS calls `{app_id}-dsn.algolia.net` with `X-Algolia-Application-Id`/`X-Algolia-API-Key` headers (key visible in Network tab — Algolia "search-only" keys are meant to be public) | POST to `https://{app_id}-dsn.algolia.net/1/indexes/*/queries`; full description is usually embedded in each search hit already |
| **Eightfold with PCSX disabled** | Tenant otherwise matches Microsoft/Morgan Stanley's Eightfold shape, but `GET /api/pcsx/search` returns 403 `"PCSX is not enabled for this user"` | Use the "related jobs" widget instead: `GET /api/apply/v2/jobs/{anchor_id}/jobs` where `{anchor_id}` is any real, currently-open job ID (hardcode one, refresh if it closes) — hard-capped at 10 results, no working pagination |
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
    - "Kochi"
    - "Kerala"
    - "Trivandrum"
    - "Lucknow"
    - "Nagpur"
    - "Madurai"
  # DO NOT add require_tech_in_description unless explicitly asked
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
| TCS iBegin: `"C#"` keyword matches all 4,227 India jobs | The `#` symbol breaks the server-side search, causing it to return everything | Use `"dotnet"` and other non-symbol keywords; rely on `require_tech_in_description` for precision |
| TCS iBegin: description endpoint requires POST not GET | `GET /candidate/api/v1/job/desc/{id}` returns 401; `POST` with `{"jobId": <int>}` body works | Strip the J/W suffix from the job ID and cast to int before POSTing |
| TCS iBegin: old domain dead | `ibegin.tcs.com` no longer resolves | Use `ibegin.tcsapps.com` |
| TCS alert links landed on the home page, not the job | Application URL used AngularJS hashbang routing (`/candidate/#!/jobs/{id}`), but the iBegin app has `html5Mode(true)` — path-based routing; hashbang URLs are silently ignored | Use path URLs: `https://ibegin.tcsapps.com/candidate/jobs/{id}` (verified rendering the job + Apply button in Playwright) |
| Infosys alert links showed a 404 page | Application URL used `/jobdetails?...` but the Angular app has no such route — its job-description route is `/jobdesc` | Use `https://career.infosys.com/jobdesc?jobReferenceCode={ref}&sourceId={id}` (verified rendering the job + Apply button in Playwright) |
| Invesco India jobs invisible to `is_india_job()` | Workday tenant IVZ has no country facet and locationsText is "Hyderabad, Telangana" — no "India" substring | Fetcher detects India via city/state token list, appends ", India"; word-boundary guard rejects "Indianapolis"/"Indiana" |
| PayPal/FactSet client-side India check risks "Indianapolis" | Plain `"india" in loc` substring matches "Indianapolis" and "Indiana" | Use regex `\bindia\b` word-boundary match in the fetcher |
| Standard Chartered/Wipro/HCLTech search results appeared empty | These SuccessFactors tenants run the newer "Job2Web Unify" theme, which loads results via a client-side JS call to `/services/recruiting/v1/jobs` — the classic J2W data-row HTML (Nomura/Capgemini) is never server-rendered | Captured the real POST via Playwright network capture: JSON body with `facetFilters`, CSRF token from a `var CSRFToken = "...";` assignment on the category/search page, session cookie from the same GET |
| Wipro/HCLTech job descriptions came back as unrelated boilerplate ("About Wipro is a leading...") | `itemprop="description"` appears twice per page on this ATS — once on a generic company blurb, once on the real job content; grabbing the first match silently returns the wrong text | Anchor extraction on the "Job Description:" joblayouttoken label instead of the itemprop attribute |
| Standard Chartered/Wipro pagination silently capped at 10 results | `/services/recruiting/v1/jobs` ignores `start`/`offset`; the only way to page is incrementing `pageNumber` in the POST body itself | Loop `pageNumber` 0,1,2… until a page returns an empty `jobSearchResult`, caching everything in-module |
| HSBC search API returns 403 "PCSX is not enabled for this user" | HSBC's Eightfold tenant (`portal.careers.hsbc.com`, migrated off the old Avature `mycareer.hsbc.com`) disabled the public `pcsx/search` endpoint that Microsoft/Morgan Stanley use | Use the "related jobs" widget endpoint (`/api/apply/v2/jobs/{anchor_id}/jobs`) instead — requires a real, currently-open job ID as a similarity anchor (hardcoded, same pattern as Wells Fargo's India WID); hard-capped at 10 results, `start`/`num` ignored |
| MSCI's own site API returned 404 | `careers.msci.com/api/jobs` doesn't exist — the site is an Algolia InstantSearch frontend calling Algolia directly, not a first-party API, despite `globalcareers-msci.icims.com` also existing (iCIMS handles applications, not search) | Call the Algolia REST endpoint directly with the public search-only API key captured from the page's network requests |
| Micron/Verizon/Lowe's "India"-faceted results were mostly non-India (Singapore, Taiwan, Boise ID, Arlington TX, Richmond VA, Charlotte NC HQ) | Assumed every Workday tenant's `locationCountry`/`Location_Country` facet is authoritative like Fidelity/Citi/Northern Trust — some tenants' facets are simply broken and return jobs from other countries anyway (Micron: ~85% leakage) | Audited all newly-added companies by fetching with the India facet applied and manually checking `locationsText` for genuine non-India place names before trusting the facet; for broken tenants, stopped blindly appending ", India" (which would mislabel a Singapore job as India) and let `matcher.py`'s `is_india_job()` reject anything that doesn't genuinely say "India" — Lowe's needed a middle ground (city-name whitelist) since it has real Bengaluru postings that never say "India" either |
| HCLTech's `require_tech_in_title` config had zero effect | `config.yaml` had the key set, comment said "MANDATORY", but `run_hcltech.py` (unlike `run_wellsfargo.py`/`run_accenture.py`/etc.) never actually read it or applied the filter — Layer 4 was silently dead since HCLTech was added | Replaced with a working `require_tech_in_description` filter checked against the description instead of the title, since HCLTech's titles are pure "Software Engineer L1/L2/L3" bands with zero tech signal. Wipro had the identical dead-config bug (`run_wipro.py` never read `require_tech_in_title` either) — fixed the same way in the same change that retired title-based matching everywhere (see next entry) |
| GitHub Actions sent the same job alert twice, 15–60 min apart | `actions/checkout` pins the commit SHA at *workflow-run creation* time, not job-*start* time. `concurrency: group: job-watcher` queues runs correctly, but frequent triggers (workflow_dispatch every ~15 min stacked on the 30-min cron) plus a ~30-45 min full run meant a queued run's checkout SHA often predated the seen_jobs commit the run ahead of it in the queue was about to push — so the queued run started from stale dedup state and re-alerted jobs already sent minutes earlier | Added a `git pull --ff-only origin master` step immediately after checkout, before any pipelines run (always a clean fast-forward — nothing local has been touched yet); also hardened the final push into a 3-attempt retry loop that fails the step loudly (`::error::`) instead of silently losing the commit if it's ever still rejected |
| Title-based Layer 4 (`require_tech_in_title`) retired across all 9 companies that used it | Verified live against HCLTech: description-based matching correctly rejected 76 non-.NET IT-ops roles (Cisco Unified Comms, ServiceNow, GCP, Azure monitoring) that a title check alone can't distinguish, since these ATSes use level-banded generic titles ("Senior Software Engineer", "SOFTWARE ENGINEER L3") with the actual tech stack named only in the JD body — title text was structurally the wrong signal for this class of company | Wells Fargo, Accenture, Infosys, Cognizant, TCS, Capgemini, Wipro, HCLTech, and DXC all now use `require_tech_in_description` (narrow core-term description match) instead. `require_tech_in_title` no longer exists anywhere in `config.yaml`; the title-matching code path was removed from every `run_<company>.py` that had it |

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
  exclude_locations:              # ALWAYS include this full default set
    - "Chennai"
    - "Tamil Nadu"
    - "Pune"
    - "Chandigarh"
    - "Kochi"
    - "Kerala"
    - "Trivandrum"
    - "Lucknow"
    - "Nagpur"
    - "Madurai"
  require_tech_in_description: [...]  # OPTIONAL Layer 4 — do not add by default
```

---

## GitHub Actions

- All 64 pipelines run in **parallel** (`& pid=$!` pattern with `wait $pid || fail=1`)
- Firefox Playwright is **cached** via `actions/cache@v4` on `~/.cache/ms-playwright`
- `seen_jobs_*.json` files are committed back after each run with `[skip ci]` to prevent re-triggering
- Workflow is triggered manually (`workflow_dispatch`) — the cron expression in the file is intentionally left as a placeholder
