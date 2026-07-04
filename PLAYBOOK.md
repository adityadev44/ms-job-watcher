# Job Watcher Playbook

Reference for maintaining this project and adding new company pipelines.

---

## What This System Does

Monitors 74 company career sites every 30 minutes via GitHub Actions. Filters for India-based .NET/C# software engineering roles and sends Telegram + email alerts only for jobs not seen before. Each company has its own fetcher, registry entry, seen-jobs file, and config section; orchestration is shared.

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

**Layer 4 — Tech in description (Wells Fargo, Accenture, Infosys, Cognizant, Capgemini, TCS, Wipro, HCLTech, DXC, Citi, State Street, First American, Adobe, Sabre, Autodesk, Micron, eBay, Oracle, Lowe's, Bank of America, LTIMindtree, Persistent Systems, Genpact, IBM, Tech Mahindra, Virtusa, Hexaware, Societe Generale, Charles Schwab — opt-in, do not add to new companies by default)**
- Description must explicitly contain a **narrow** .NET/C#/ASP.NET term (`require_tech_in_description` in config) — narrower than Layer 3's `primary_skills`, which also passes on SQL Server/EF/Web API alone
- Originally added for IT-services shops whose generic titles ("Senior Software Engineer", "Software Engineer L3") give no reliable tech signal, where Layer 3's broader skill list was letting non-.NET roles through (e.g. HCLTech: Cisco Unified Comms, ServiceNow, GCP, Azure-monitoring roles that happened to mention SQL Server/EF); since extended to direct employers (Citi, State Street, First American, Adobe, Sabre, Autodesk, Micron, eBay, Oracle, Lowe's, Bank of America, LTIMindtree, Persistent Systems, Genpact, IBM, Tech Mahindra, Virtusa, Hexaware, Societe Generale, Charles Schwab) as a general precision tightener wherever the broader skill list alone risks false positives
- Activated by the company's registry metadata and applied centrally by `run_company.py` after the shared matcher.
- **Retired: title-based matching (`require_tech_in_title`).** Every company that used it now uses description matching instead — title text turned out to be too sparse a signal at IT-services shops (many real .NET roles carry a generic level-banded title with the tech named only in the JD body)

Deduplication: jobs already in `seen_jobs_<company>.json` are never re-alerted (all companies).

---

## Architecture

```
config.yaml                    ← all config (search params + shared matching rules)
src/
  company_registry.py          ← inventory + conservative fetch capabilities
  run_company.py               ← one generic pipeline implementation
  run_all.py                   ← bounded launcher (10 at a time by default)
  matcher.py                   ← shared filter engine (title family → exclude → skills)
  notifier.py                  ← Telegram + email alerts
  <company>_fetcher.py         ← data source: fetch_jobs() + fetch_job_description()
seen_jobs_<company>.json       ← deduplication state
.github/workflows/watcher.yml  ← invokes the launcher and commits state
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
        └─ known-ID check              skip detail call for alerted jobs
        └─ fetch_job_description()
            └─ primary_skills check    [broad-only] / [react-only] / [skill] tags
```

**Layer 4 (opt-in, never added by default):**
```
    └─ require_tech_in_description check [desc-tech] tag in near-miss log
```
Implemented once in `run_company.py`; registry metadata enables it for companies whose config contains `require_tech_in_description`.

---

## Current Companies

| Company | ATS | Fetch method | Registry slug | Notes |
|---|---|---|---|---|
| Microsoft | Eightfold | REST API (JSON) | `microsoft` | Original pipeline |
| Optum | TalentBrew | HTML scraping | `optum` | |
| Amazon | Custom | REST API (JSON) | `amazon` | |
| Siemens | Custom | HTML scraping | `siemens` | Keywords ignored server-side; fetches all, dedupes |
| Honeywell | Oracle HCM CE | **Playwright/Firefox** | `honeywell` | Chromium blocked by Akamai; titles use "Engr" not "Engineer" |
| Wells Fargo | Workday | REST API (JSON) | `wellsfargo` | India WID hardcoded; `require_tech_in_description` 4th filter active |
| Dell | Custom | REST API (JSON) | `dell` | |
| Oracle | Oracle HCM CE | REST API (JSON) | `oracle` | `require_tech_in_description` active |
| MetLife | Custom | REST API (JSON) | `metlife` | Empty keyword fetches all India jobs; title/skills filter handles rest |
| FIS | Custom | REST API (JSON) | `fis` | |
| Chubb | Oracle HCM CE | REST API (JSON) | `chubb` | Tenant: fa-ewgu-saasfaprod1.fa.ocs.oraclecloud.com, site CX_2001 |
| S&P Global | Workday | REST API (JSON) | `spglobal` | No India facet; fetches globally, filters client-side; state-name normalisation for India detection |
| WTW | Oracle HCM CE | REST API (JSON) | `wtw` | Tenant: eedu.fa.em3.oraclecloud.com, site CX_1003; India facet unreliable — fetches globally, filters client-side |
| Morningstar | Phenom People | Sitemap + HTML scraping | `morningstar` | `/widgets` API not accessible without browser JS; sitemap has 208 jobs; each page's JSON-LD has full description — all fetched once and cached in-module |
| S&P Global Careers | iCIMS | REST API (JSON) | `spglobal_careers` | Separate portal from Workday pipeline (`careers.spglobal.com/api/jobs`); full description included in search response — no detail fetch needed |
| Gallagher (AJG) | iCIMS | REST API (JSON) | `gallagher` | `jobs.ajg.com/api/jobs`; identical iCIMS pattern to S&P Global Careers; India jobs in Kochi |
| Icertis | Oracle HCM CE | REST API (JSON) | `icertis` | Tenant: iaaviz.fa.ocs.oraclecloud.com, site Jobs-at-Icertis; no India facet — fetches globally, filters client-side; all current India jobs are in Pune (excluded) so 0 matches expected until Icertis opens non-Pune roles |
| Maersk | Workday | REST API (JSON) | `maersk` | `maersk.wd3.myworkdayjobs.com`; India location WIDs embedded as constant; all 122 India jobs fetched and cached once; `careers.maersk.com` API skipped (requires Consumer-Key and only returns 150 non-tech India jobs) |
| Nomura | SAP SuccessFactors J2W | HTML scraping | `nomura` | `careers.nomura.com/Nomura/go/Career-Opportunities-India/9050900/`; 337 India jobs; pagination via path segments (`/9050900/100/`, `/9050900/200/`), NOT `?startRow=N`; location format "Mumbai, IN" normalised to "Mumbai, India"; mostly Java/Python roles — .NET matches rare |
| American Express | Oracle HCM CE | REST API (JSON) | `amex` | Tenant: egug.fa.us2.oraclecloud.com, site CX_1; India location facet applied server-side |
| Fidelity | Workday | REST API (JSON) | `fidelity` | `fmr.wd1.myworkdayjobs.com`; India `locationCountry` facet; appends ", India" when missing from locationsText (city-only strings like "Bangalore, Karnataka") |
| Fiserv | Workday | REST API (JSON) | `fiserv` | `fiserv.wd5.myworkdayjobs.com`; no country facet — fetches globally, filters client-side |
| Goldman Sachs | Oracle HCM CE | REST API (JSON) | `goldmansachs` | Tenant: hdpc.fa.us2.oraclecloud.com, site LateralHiring; application_url points to public higher.gs.com (GraphQL BFF requires Okta auth; raw Oracle tenant is open) |
| JPMorgan Chase | Oracle HCM CE | REST API (JSON) | `jpmorgan` | Tenant: jpmc.fa.oraclecloud.com, site CX_1001; India location facet ID 300000000289360 |
| Marsh McLennan | Workday | REST API (JSON) | `marshmclennan` | `mmc.wd1.myworkdayjobs.com`; `Location_Country` facet (capitalised key, unlike other Workday tenants); appends ", India" when missing from locationsText |
| Mastercard | Workday | REST API (JSON) | `mastercard` | `mastercard.wd1.myworkdayjobs.com`; no country facet — uses "locations" facet with 8 India city WIDs; appends ", India" for "2 Locations" entries |
| Morgan Stanley | Eightfold | REST API (JSON) | `morganstanley` | `morganstanley.eightfold.ai`; same Eightfold PCSX API shape as Microsoft pipeline |
| Nagarro | SmartRecruiters | REST API (JSON) | `nagarro` | `careers.smartrecruiters.com/nagarro1`; `country=in` param filters server-side; keyword param is a loose pre-filter only (titles still need matcher's title-family check) |
| Citi | Workday | REST API (JSON) | `citi` | Tenant: `citi.wd5.myworkdayjobs.com`, site `2`; `Country_and_Jurisdiction` facet (not `locationCountry`); India WID `c4f78be1a8f14da0ab49ce1162348a5e`; ~339 India SW engineer jobs; "2 Locations" entries appended ", India" client-side; `require_tech_in_description` active |
| BNY Mellon | Oracle HCM CE | REST API (JSON) | `bny` | Tenant: `eofe.fa.us2.oraclecloud.com`, site `BNY-Careers`; India location facet ID `300000000378365`; majority of India jobs are Pune (excluded) — 0 matches expected until non-Pune .NET roles open |
| Northern Trust | Workday | REST API (JSON) | `northerntrust` | Tenant: `ntrs.wd1.myworkdayjobs.com`, site `northerntrust`; `locationCountry` WID `c4f78be1a8f14da0ab49ce1162348a5e` (same cross-tenant India GUID as Fidelity/Wells Fargo); page size capped at 20 |
| Deutsche Bank | Beesite + Workday | Beesite REST API (JSON) + Workday CXS descriptions | `deutsche` | Keywords and country filter ignored server-side; fetches all ~1808 global jobs, filters India (`CountryCode==IN`) client-side; descriptions via Workday CXS at `db.wd3.myworkdayjobs.com` |
| Barclays | Workday | REST API (JSON) | `barclays` | Tenant: `barclays.wd3.myworkdayjobs.com`; no `locationCountry` facet — uses 11 India city WIDs in `appliedFacets.locations`; `locationsText` omits "India" — appended client-side |
| UBS | IBM BrassRing | REST API (JSON) | `ubs` | `jobs.ubs.com`; CSRF token (`RFT` header) required per session — GET page first; descriptions inline in search results; ~15 India jobs; pagination wraps around — stop when no new IDs |
| Accenture | Workday (wd103) | REST API (JSON) | `accenture` | Tenant: `accenture.wd103.myworkdayjobs.com/AccentureCareers`; India WID `c4f78be1a8f14da0ab49ce1162348a5e`; ~24,731 India jobs; API caps at 2000 per query; `require_tech_in_description` active — essential |
| Infosys | Custom (in-house) | REST API (JSON) | `infosys` | `intapgateway.infosysapps.com/careersci/`; `searchText` is a no-op — all ~1558 India jobs returned per call; descriptions bundled in list response (no detail fetches); `require_tech_in_description` active; **very fast job turnover** — links can go stale (site silently shows a generic list, no error) within days if the posting closes; see Key Bugs |
| Cognizant | Custom (Umbraco CMS) | RSS feed (XML) | `cognizant` | RSS at `careers.cognizant.com/global-en/jobs/xml/?rss=true` returns all ~2069 jobs with full descriptions — no per-job fetch needed; India filtered client-side via `<country>` field; `require_tech_in_description` active |
| Capgemini | SAP SuccessFactors J2W | HTML scraping | `capgemini` | `careers.capgemini.com`; same J2W platform as Nomura but `?startrow=N` query-string pagination (not path-based); `locationsearch=india` server-side; location "City, IN" normalised to "City, India"; `require_tech_in_description` active |
| TCS | iBegin (proprietary) | REST API (JSON) | `tcs` | `ibegin.tcsapps.com`; POST `/candidate/api/v1/jobs/searchJ`; India-only portal; 10 jobs/page (fixed); keyword `#` breaks search — `"C#"` matches all 4,227 India jobs; use `"csharp"` or `"dotnet"` instead; apply-by date used as posting date proxy; `require_tech_in_description` active |
| Synchrony | Workday | REST API (JSON) | `synchrony` | `synchronyfinancial.wd5.myworkdayjobs.com`; no country facet — uses "locations" facet with 6 India WIDs (Hyderabad + 5 Remote IN regions) |
| LSEG | Workday | REST API (JSON) | `lseg` | `lseg.wd3.myworkdayjobs.com`, site `careers`; `locationCountry` facet; large Bengaluru centre, frequent .NET roles; office-code locations ("IND-BLR-…") get ", India" appended |
| State Street | Workday | REST API (JSON) | `statestreet` | `statestreet.wd1.myworkdayjobs.com`, site `Global`; `Location_Country` facet (capitalised, like MMC); ~200 India jobs; Coimbatore excluded by name (location text omits "Tamil Nadu"); `require_tech_in_description` active |
| Broadridge | Workday | REST API (JSON) | `broadridge` | `broadridge.wd5.myworkdayjobs.com`, site `Careers`; `Location_Country` facet; .NET-heavy shop, ~30 India jobs (Bengaluru/Hyderabad) |
| Kyndryl | Workday | REST API (JSON) | `kyndryl` | `kyndryl.wd5.myworkdayjobs.com`, site `KyndrylProfessionalCareers`; `locationCountry` facet; ~290 India software jobs |
| DXC Technology | Workday | REST API (JSON) | `dxc` | `dxctechnology.wd1.myworkdayjobs.com`, site `DXCJobs`; `locationCountry` facet; locations use state codes ("IND - TN - CHENNAI") — "- TN -" added to exclude_locations to cover all Tamil Nadu cities; `require_tech_in_description` active — IT-services generic titles |
| Ameriprise | Workday | REST API (JSON) | `ameriprise` | `ameriprise.wd5.myworkdayjobs.com`, site `ameriprise`; `locationCountry` facet; Hyderabad/Noida/Gurugram |
| FactSet | Workday | REST API (JSON) | `factset` | `factset.wd108.myworkdayjobs.com`, site `FactSetCareers`; no country facet — global fetch (~60 jobs) + client-side India filter; .NET-heavy Hyderabad centre |
| PayPal | Workday | REST API (JSON) | `paypal` | `paypal.wd1.myworkdayjobs.com`, site `jobs`; no country facet — global fetch + word-boundary India filter (`\bindia\b`, so "Indianapolis" never passes); small India presence, 0 matches often expected |
| Invesco | Workday | REST API (JSON) | `invesco` | `invesco.wd1.myworkdayjobs.com`, site `IVZ`; no country facet AND locationsText omits "India" ("Hyderabad, Telangana") — India detected via city/state tokens; .NET-heavy Hyderabad centre |
| First American | Workday | REST API (JSON) | `firstamerican` | `firstam.wd1.myworkdayjobs.com`, site `faicareers` — First American India's dedicated portal, every posting is India (all Bangalore); heavily .NET shop; ", India" appended to "IND, Karnataka, Bangalore" locations; `require_tech_in_description` active |
| Standard Chartered | SAP SuccessFactors (Job2Web Unify) | REST API (JSON) | `standardchartered` | `jobs.standardchartered.com`, categoryId `9783657`; CSRF-token + session-cookie handshake; `facetFilters.jobLocationCountry` restricts to India (~280 jobs); keywords/location ignored server-side |
| Wipro | SAP SuccessFactors (Job2Web Unify) | REST API (JSON) | `wipro` | `careers.wipro.com`, categoryId `0`; same Unify pattern as Standard Chartered; ~3700 India jobs; `require_tech_in_description` active — see Key Bugs, was previously a dead `require_tech_in_title` config that `wipro` never read; generic "SOFTWARE ENGINEER L3/L4" titles dominate |
| HCLTech | SAP SuccessFactors (Job2Web Unify) | REST API (JSON) | `hcltech` | `careers.hcltech.com`, India-only categoryId `9553955`; same Unify pattern, different field names (`custprimecity`/`custCountryRegion` instead of `jobLocationShort`/`jobLocationCountry`); ~8000 India jobs; `require_tech_in_description` active — see Key Bugs, was previously a dead `require_tech_in_title` config |
| HSBC | Eightfold | REST API (JSON) | `hsbc` | `portal.careers.hsbc.com` (migrated off the old Avature `mycareer.hsbc.com` portal); public `pcsx/search` API disabled tenant-wide — uses the "related jobs" widget endpoint anchored to a hardcoded real job ID; hard-capped at 10 results, no pagination |
| MSCI | Algolia (direct) | REST API (JSON) | `msci` | `careers.msci.com` frontend queries Algolia directly (app `RVMOB42DFH`) with a public search-only API key; ~90 jobs total, ~18 India, single unfiltered query covers everything; full description embedded in each hit — no detail fetch needed |
| Target | Workday | REST API (JSON) | `target` | `target.wd5.myworkdayjobs.com`, site `targetcareers`; India GCC in Bangalore; capitalised `Location_Country` facet; ~58 India software-engineer jobs |
| Adobe | Workday | REST API (JSON) | `adobe` | `adobe.wd5.myworkdayjobs.com`, site `external_experienced`; India centres in Bangalore + Noida; `locationCountry` facet works despite not being listed in the tenant's advertised facets; `require_tech_in_description` active |
| Micron | Workday | REST API (JSON) | `micron` | `micron.wd1.myworkdayjobs.com`, site `External`; Hyderabad "Phoenix Aquila" campus; semiconductor/hardware — low .NET match volume expected; **`locationCountry` facet unreliable (~85% non-India leakage)** — fetcher does not append ", India", relies on `is_india_job()` to filter genuinely; `require_tech_in_description` active |
| Sabre | Workday | REST API (JSON) | `sabre` | `sabre.wd1.myworkdayjobs.com`, site `SabreJobs`; travel-technology (GDS) company, Bengaluru engineering centre; `require_tech_in_description` active |
| Autodesk | Workday | REST API (JSON) | `autodesk` | `autodesk.wd1.myworkdayjobs.com`, site `Ext`; India centres in Bengaluru + Pune; locationsText uses abbreviated "IND" country code; `require_tech_in_description` active |
| Verizon | Workday | REST API (JSON) | `verizon` | `verizon.wd12.myworkdayjobs.com`, site `verizon-careers`; Hyderabad network/telecom engineering; **`locationCountry` facet unreliable** (genuine US locations leak through) — fetcher does not append ", India" |
| Lowe's | Workday | REST API (JSON) | `lowes` | `lowes.wd5.myworkdayjobs.com`, site `LWS_External_CS`; Bengaluru engineering centre; **`locationCountry` facet unreliable** (US locations leak through) — fetcher appends ", India" only for recognised India city names, not blindly; `require_tech_in_description` active |
| eBay | Workday | REST API (JSON) | `ebay` | `ebay.wd5.myworkdayjobs.com`, site `apply`; Bengaluru engineering centre; capitalised `Location_Country` facet; `require_tech_in_description` active |
| General Motors | Workday | REST API (JSON) | `generalmotors` | `generalmotors.wd5.myworkdayjobs.com`, site `Careers_GM`; GM India Technical Centre in Bengaluru — small footprint (~5 total postings), 0 matches often expected; capitalised `Location_Country` facet |
| Bank of America | Custom (AEM + in-house servlet) | REST API (JSON) | `bankofamerica` | `careers.bankofamerica.com/services/jobssearchservlet`; keyword/country params ignored server-side — ~1760 global jobs cached, ~62 India (Mumbai/Hyderabad/Chennai/Gurugram); description is plain server-rendered HTML, no Playwright needed; `require_tech_in_description` active |
| LTIMindtree | RippleHire | REST API (JSON) | `ltimindtree` | Public `careers.ltimindtree.com` search has ~80 overseas-only roles, zero India — actual India hiring is on a separate RippleHire site (`ltimindtree.ripplehire.com`, linked from `ltm.com/india-careers`); `search` keyword IS applied server-side (unlike most fetchers here); posting date only available via detail fetch; `require_tech_in_description` active |
| Persistent Systems | Zwayam | REST API (JSON) | `persistent` | `apipersistent.zwayam.com`; server hard-caps pagination at 9/page regardless of requested size — ~700 global jobs cached in ~78 requests; `anyOfTheseWords` keyword filter is a noisy OR-match, ignored; search results' `location` field is country-only ("India") — city parsed from the `jobUrl` slug (`...-india-pune-...`) so Pune/Chennai exclusions work; `require_tech_in_description` active |
| Genpact | Workday | REST API (JSON) | `genpact` | Tenant `genpact.wd108.myworkdayjobs.com`, site `External_Careers`; no usable location facet (`locationMainGroup` has no India sub-value) — global fetch per keyword + client-side India filter via `locationsText`; `require_tech_in_description` active |
| IBM | Custom (Elasticsearch) | REST API (JSON) + **Playwright/Firefox** for descriptions | `ibm` | `www-api.ibm.com/search/api/v2`; India filtered server-side via `field_keyword_05` term facet (discovered from the response's own aggregations); job-detail pages (`careers.ibm.com/careers/JobDetail`) sit behind AWS WAF bot-challenge tokens — plain `requests` gets 202/empty body, so description fetch uses headless Firefox (same pattern as Honeywell); no posting-date field exposed anywhere; `require_tech_in_description` active |
| Tech Mahindra | Legacy ASP.NET WebForms | **Playwright/Firefox** for search, REST-ish HTML for descriptions | `techmahindra` | `careers.techmahindra.com`; country-select fires an AJAX UpdatePanel partial postback needing exact ViewState/EventValidation/X-MicrosoftAjax headers to replicate — drives headless Firefox instead, calling `__doPostBack` via `page.evaluate()` to sidestep a cookie-consent overlay blocking real clicks; ~81 India jobs; job-detail pages ARE plain server-rendered HTML (no browser needed there); `require_tech_in_description` active |
| Virtusa | Taleo | REST API (JSON) for search + **Playwright/Firefox** for descriptions | `virtusa` | `virtusa.taleo.net`; search REST endpoint needs TZ/tzname/X-Requested-With headers matching the browser or returns raw HTTP 500; India via LOCATION facet id `200100250` (~720 jobs); job-detail description is a ~13KB JSF-postback form too fragile to hand-replicate — uses headless Firefox instead; `require_tech_in_description` active |
| Hexaware | Oracle HCM CE | REST API (JSON) | `hexaware` | Tenant `fa-etqo-saasfaprod1.fa.ocs.oraclecloud.com`, site `CX_1`; same REST pattern as Chubb/Amex/Icertis, plain requests, no Playwright; no location facet ID used — fetches globally, filters India client-side via `PrimaryLocation` (Icertis/WTW pattern); `require_tech_in_description` active |
| Societe Generale | Custom (Exalead/CloudView) | REST API — server-rendered HTML fallback | `societegenerale` | `careers.societegenerale.com`; the site's own search-proxy API needs a bearer token that 403s outside the page's own first-load request (even replayed from within the same browser session) — instead uses the fully server-rendered `/en/Technical/all-job-offers` listing page (all ~694 postings on one page, no pagination); job-detail pages carry a clean schema.org JobPosting JSON-LD block; `require_tech_in_description` active |
| Charles Schwab | iCIMS (backend) + plain HTML frontend | REST API — server-rendered HTML | `schwab` | `www.schwabjobs.com`; Apply flow goes through `career-ind-schwab.icims.com` but search/listing pages are plain server-rendered HTML, no API needed; India via `/search-jobs/india` URL path (~19 jobs, small Hyderabad presence); job-detail pages carry a schema.org JobPosting JSON-LD block; `require_tech_in_description` active |

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

### Step 4 — Register the company

Add one `CompanyPipeline` row to `src/company_registry.py` through `_PIPELINE_DATA`. The slug drives the conventional fetcher, config, and state names. Set the display source and whether the narrow description filter is enabled.

Also classify only capabilities you verified:

- Add the slug to `_IGNORES_KEYWORDS` when the API returns the same pool for every query. This makes the generic runner issue one query pass instead of six identical passes.
- Add it to `_SUPPORTS_LOCATION`, `_INLINE_DESCRIPTIONS`, or `_NEWEST_FIRST` only after observing that behavior.
- Conservative defaults are intentional. Never use alerted-job IDs as a pagination watermark: they do not include filtered-out listings, and config changes can make old jobs newly eligible.

### Step 5 — Add to `config.yaml`

Add a new section before `notifications:`:
```yaml
<company>_search:
  max_listings: 200
  inter_page_delay: 0.2       # be polite; 0.1 for fast APIs
  keywords: *default_keywords
  locations: *india_locations
  exclude_locations: *default_exclude_locations
  # DO NOT add require_tech_in_description unless explicitly asked
```

Check if the company's ATS uses server-side keyword filtering (Workday does) or ignores keywords (Siemens doesn't). If it ignores keywords, all keywords will return the same result set and deduplication will handle it.

### Step 6 — Leave the workflow alone

The workflow calls `run_all.py`, which discovers the registry, and stages tracked `seen_jobs*.json` files with a pathspec. Adding a registry entry and state file is enough. If the company needs Playwright, Firefox is already installed and cached.

### Step 7 — Create `seen_jobs_<company>.json`

```json
[]
```

Commit this file alongside everything else.

### Step 8 — Test locally, then push

```bash
py src/run_all.py --validate --companies <company>
py src/run_company.py <company>
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
| Infosys links reported as "stale" (2026-07-03 re-investigation) | Confirmed via Playwright that `/jobdesc?jobReferenceCode=...` still renders the correct job detail (Job ID, Responsibilities, Apply — it just sits below a long "similar jobs" sidebar, which reads as broken at a glance). Cross-checked all 8 IDs in `seen_jobs_infosys.json` against a fresh live fetch: 4 were no longer in the listing. Loading one of those closed IDs' URL confirms the site silently falls back to the generic sidebar list with **no error state at all** — nothing distinguishes "closed" from "loading". Root cause is Infosys's own ~1500-job portal cycling postings very fast (some of our own seen IDs closed within days), not a URL-construction bug on our side | Not fixable from our end — Infosys's site gives no signal to detect closure before rendering. No code change; documented here so this isn't re-investigated as a bug. If it recurs, alerts sent promptly (30-min cycle) are the only mitigation available |
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
| Title-based Layer 4 (`require_tech_in_title`) retired across all 9 companies that used it | Verified live against HCLTech: description-based matching correctly rejected 76 non-.NET IT-ops roles (Cisco Unified Comms, ServiceNow, GCP, Azure monitoring) that a title check alone can't distinguish, since these ATSes use level-banded generic titles ("Senior Software Engineer", "SOFTWARE ENGINEER L3") with the actual tech stack named only in the JD body — title text was structurally the wrong signal for this class of company | Wells Fargo, Accenture, Infosys, Cognizant, TCS, Capgemini, Wipro, HCLTech, and DXC all now use `require_tech_in_description` (narrow core-term description match) instead. `require_tech_in_title` no longer exists anywhere in `config.yaml`; the old per-company code path was removed and the rule is now centralized in `run_company.py` |
| LTIMindtree's public careers.ltimindtree.com search returned zero India jobs | The domain's `/search/` results (SAP SuccessFactors classic J2W, same platform as Nomura/Capgemini) only cover ~80 overseas lateral roles — genuine India hiring never appears there at all, not even with correct `q`/`locationsearch` params | Found the real India portal via web search: `ltimindtree.ripplehire.com` (RippleHire ATS), linked from `ltm.com/india-careers` ("Opportunities in India") — LTIMindtree rebranded to "LTM" and the India-specific hiring flow moved off the classic SuccessFactors site entirely |
| Persistent Systems' Zwayam search results have no city in `location` (just "India") | Would let Pune (Persistent's HQ, heaviest posting volume) and other excluded cities silently bypass `exclude_locations`, since the string never contains a city name to match against | The `jobUrl` slug reliably embeds `india-{city}-{timestamp}` (e.g. `programmer-dev-india-pune-2026060717164612`) — parsed via regex into `"{City}, India"` before handing off to matcher.py |
| IBM job-detail pages return HTTP 202 with an empty body to plain `requests` | `careers.ibm.com/careers/JobDetail` is behind AWS WAF bot-challenge tokens (`token.awswaf.com`) that a script can't solve without executing the page's JS | Description fetch uses headless Firefox via Playwright (same approach as Honeywell) — a real browser executing the WAF's JS solves the challenge transparently and the full JD renders normally |
| Genpact (Workday) has no usable location facet | The only facets Workday exposes for this tenant are `jobFamilyGroup`/`workerSubType`/`timeType`/`locationMainGroup`, and `locationMainGroup`'s own values list comes back as a single placeholder ("Locations" with a null id) — no India WID to apply server-side | Fetch globally per keyword (searchText narrows the ~2000-job-capped total significantly) and filter India client-side via `locationsText`, same pattern as Fiserv/FactSet |
| `persistent_fetcher.py` (and `bankofamerica_fetcher.py`, masked by luck) re-fetched their entire job pool on every call | Copied the Deutsche Bank cache-once pattern but dropped the `if _cache_filled: return` early-exit guard inside `_fill_cache()` — only the module-level flag assignment was copied, not the check. Persistent's 78-page pagination re-ran on every one of matcher.py's ~30+ per-keyword calls (~15,000 requests), a test run took 21 minutes before being killed. Bank of America's version had the identical bug but its cache fill is a single request, so it silently wasted ~30 redundant requests instead of visibly hanging | Added the missing `if _cache_filled: return` guard to both. Any new cache-once fetcher must be tested for wall-clock time, not just correctness — a working-but-slow fetcher can look identical to a hung one until you check how long it actually took |
| Tech Mahindra: hand-built ASP.NET UpdatePanel postback returned a raw 500 error page | POST to `CurrentOpportunity.aspx` with `__EVENTTARGET`/`__VIEWSTATE`/`__EVENTVALIDATION` copied from a plain GET was missing the `X-MicrosoftAjax: Delta=true` header and the `ToolkitScriptManager1` field identifying which UpdatePanel triggered the postback — ASP.NET AJAX requires both or it can't route the partial-postback response | Confirmed via Playwright network capture of the real request. Rather than reverse-engineer the pipe-delimited AJAX "delta" response format too, switched to driving headless Firefox through the country-select + pagination flow directly (same approach as Honeywell) |
| Virtusa (Taleo) job-detail description came back empty | The description-populating `jobdetail.ajax` POST carries ~100 JSF-style form fields (~13KB body, ViewState-like `initialHistory`/`cshtstate` fields) in a pipe-delimited response — effectively internal Taleo view state, not a stable API contract | Search stayed on Taleo's clean REST endpoint (`rest/jobboard/searchjobs`, which just needed matching TZ/tzname/X-Requested-With headers); description fetch uses headless Firefox navigating directly to `jobdetail.ftl?job={id}`, which renders standalone without needing prior search-flow session state |
| Societe Generale's own search API (`search-proxy.php`) returned 403 even when replayed from inside the same authenticated Playwright page via `fetch()` | The site's `get-token` bearer-token endpoint appears to validate something about the original page-load request context that a same-session `fetch()` call afterward doesn't reproduce (possibly single-use nonce or strict Sec-Fetch/timing checks) — not just a missing-header problem | Found that `/en/Technical/all-job-offers` is fully server-rendered — a plain unauthenticated GET returns all ~694 postings embedded in the HTML directly, sidestepping the token flow entirely |

---

## Config Reference

```yaml
_defaults:                         # YAML anchors; parsed values stay ordinary lists
  keywords: &default_keywords [...]
  india_locations: &india_locations ["India"]
  exclude_locations: &default_exclude_locations [...]

matching:                         # shared across ALL companies
  title_family: [...]             # titles that pass (e.g. "software engineer")
  exclude_terms: [...]            # titles that always fail (managers, interns, etc.)
  skills: [...]                   # at least one must appear in description
  primary_skills: [...]           # at least one of THESE must appear (no Azure-only pass)

<company>_search:                 # per-company, fully isolated
  max_listings: 200
  inter_page_delay: 0.2
  keywords: *default_keywords
  locations: *india_locations
  exclude_locations: *default_exclude_locations
  require_tech_in_description: [...]  # OPTIONAL Layer 4 — do not add by default
```

---

## GitHub Actions

- `run_all.py` runs all 74 registry entries with **bounded concurrency** (10 workers by default; configurable with `--workers` or `JOB_WATCHER_WORKERS`)
- One company failure does not cancel peers, but the launcher exits non-zero after all work finishes so the workflow is visibly failed
- Firefox Playwright is **cached** via `actions/cache@v4` on `~/.cache/ms-playwright`
- Tracked `seen_jobs*.json` and `pipeline_failures.json` changes are committed after each run with `[skip ci]`, including failed runs
- The workflow runs every 30 minutes and also supports manual `workflow_dispatch`
- Queued runs fast-forward before scanning; state pushes retain the union merge driver and three-attempt rebase/push loop

---

## Optimization Learnings (2026-07-04)

- **Filter known IDs before detail fetches.** The alert ledger already guarantees these jobs cannot notify again, so re-downloading their descriptions was pure cost. Keep the check after cheap location/title filters for useful diagnostics, but before detail HTTP.
- **Do not stop pagination using alert IDs.** Seen files contain only alerted matches, not every processed listing. They are not scan watermarks, and older filtered jobs may become eligible after rule changes.
- **Model ignored query parameters explicitly.** Eighteen adapters ignore caller keywords. Registry capability metadata now collapses those to one query pass without weakening the conservative default for other adapters.
- **Bound pipeline concurrency globally.** Seventy-four simultaneous processes caused unnecessary memory/network pressure and made shared failure-state writes race. A ten-worker thread pool keeps independent pipelines moving while bounding load.
- **Delivery and dedup state are one transaction.** If every configured notification channel fails, do not advance seen state. Otherwise a transient Telegram/Gmail outage permanently loses the alert. With no channels configured, local runs retain historical state-advance behavior.
- **Keep failure state thread-safe and failures visible.** Failure-counter read/modify/write is locked; individual errors are recorded while the launcher continues, then the overall process exits non-zero.
- **Centralize orchestration, not ATS behavior.** The registry and generic runner removed 73 duplicated entry points. Fetchers remain separate because their pagination, authentication, bot protection, and location semantics genuinely differ.
- **Use YAML anchors for repeated policy.** Shared keywords, India location, and exclusions now have one source while company-specific overrides remain explicit. Parsed config was compared before/after and is identical.
