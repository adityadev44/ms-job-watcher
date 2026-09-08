"""Declarative inventory for every job-watcher pipeline.

Keeping metadata in one place makes the pipeline/config/state relationship
testable without importing every fetcher and lets :mod:`run_company` replace
dozens of copy-pasted entry points.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal


DescriptionFilter = Literal["none", "require_any_configured_term"]


@dataclass(frozen=True, slots=True)
class CompanyPipeline:
    slug: str
    source: str
    fetcher_module: str
    config_key: str
    seen_file: str
    description_filter: DescriptionFilter = "none"
    supports_keyword_filter: bool = True
    supports_location_filter: bool = False
    description_inline: bool = False
    newest_first: bool = False
    uses_playwright: bool = False

    @property
    def requires_tech_in_description(self) -> bool:
        return self.description_filter == "require_any_configured_term"


# slug, alert/display source, strict description filter.  The remaining names
# follow the conventions used by all 73 non-Microsoft adapters.
_PIPELINE_DATA = (
    ("abinbev", "AB InBev GCC", False),
    ("accenture", "Accenture", True),
    ("adityabirla", "Aditya Birla Group", False),
    ("adobe", "Adobe", True),
    ("aig", "AIG", False),
    ("airindia", "Air India", False),
    ("airtel", "Bharti Airtel", False),
    ("akasaair", "Akasa Air", False),
    ("alegeus", "Alegeus", False),
    ("allianztech", "Allianz Technology", False),
    ("alphasense", "AlphaSense", False),
    ("amazon", "Amazon", False),
    ("amdocs", "Amdocs", False),
    ("ameriprise", "Ameriprise", False),
    ("amex", "American Express", False),
    ("anz", "ANZ", False),
    ("aon", "Aon", False),
    ("apple", "Apple", False),
    ("appliedsystems", "Applied Systems", False),
    ("arcesium", "Arcesium", False),
    ("astrazeneca", "AstraZeneca", False),
    ("atlassian", "Atlassian", False),
    ("autodesk", "Autodesk", True),
    ("automationanywhere", "Automation Anywhere", False),
    ("axisbank", "Axis Bank", False),
    ("bankofamerica", "Bank of America", True),
    ("barclays", "Barclays", False),
    ("birlasoft", "Birlasoft", False),
    ("blackrock", "BlackRock", False),
    ("bloomberg", "Bloomberg", False),
    ("bnpparibas", "BNP Paribas", False),
    ("bny", "BNY Mellon", False),
    ("boeing", "Boeing", False),
    ("broadridge", "Broadridge", False),
    ("browserstack", "BrowserStack", False),
    ("canva", "Canva", False),
    ("capgemini", "Capgemini", True),
    ("chubb", "Chubb", False),
    ("cisco", "Cisco", False),
    ("citi", "Citi", True),
    ("citiustech", "CitiusTech", True),
    ("clearwater", "Clearwater Analytics", False),
    ("clevertap", "CleverTap", False),
    ("cloudflare", "Cloudflare", False),
    ("coforge", "Coforge", False),
    ("cognizant", "Cognizant", True),
    ("cred", "CRED", False),
    ("crisil", "CRISIL", False),
    ("cyient", "Cyient", False),
    ("darwinbox", "Darwinbox", False),
    ("databricks", "Databricks", False),
    ("datarobot", "DataRobot", False),
    ("dazn", "DAZN", False),
    ("definitivehealthcare", "Definitive Healthcare", False),
    ("dell", "Dell Technologies", False),
    ("deloitteusi", "Deloitte USI", False),
    ("delta", "Delta Air Lines", False),
    ("deltatre", "Deltatre", False),
    ("deltek", "Deltek", False),
    ("deutsche", "Deutsche Bank", False),
    ("disney", "Disney", False),
    ("dover", "Dover", False),
    ("dtcc", "DTCC", False),
    ("dxc", "DXC Technology", True),
    ("ebay", "eBay", True),
    ("eclerx", "eClerx", True),
    ("energyexemplar", "Energy Exemplar", False),
    ("epam", "EPAM Systems", False),
    ("eurofins", "Eurofins", False),
    ("expedia", "Expedia Group", False),
    ("eygds", "EY GDS", False),
    ("factset", "FactSet", False),
    ("fico", "FICO", False),
    ("fidelity", "Fidelity", False),
    ("firstamerican", "First American", True),
    ("fis", "FIS Global", False),
    ("fiserv", "Fiserv", False),
    ("flipkart", "Flipkart", False),
    ("franklintempleton", "Franklin Templeton", False),
    ("freshworks", "Freshworks", False),
    ("gallagher", "Gallagher", False),
    ("geaerospace", "GE Aerospace", False),
    ("gehealthcare", "GE HealthCare", False),
    ("generalmotors", "General Motors", False),
    ("genpact", "Genpact", True),
    ("gitlab", "GitLab", False),
    ("glean", "Glean", False),
    ("globallogic", "GlobalLogic", False),
    ("globant", "Globant", False),
    ("goldmansachs", "Goldman Sachs", False),
    ("google", "Google", False),
    ("groww", "Groww", False),
    ("guidewire", "Guidewire Software", False),
    ("happiestminds", "Happiest Minds", False),
    ("hartford", "The Hartford", False),
    ("hcltech", "HCLTech", True),
    ("hdfcbank", "HDFC Bank", False),
    ("healthedge", "HealthEdge", False),
    ("hexaware", "Hexaware", True),
    ("honeywell", "Honeywell", False),
    ("hsbc", "HSBC", False),
    ("ibm", "IBM", True),
    ("ice", "ICE", False),
    ("icertis", "Icertis", False),
    ("icicibank", "ICICI Bank", False),
    ("indigo", "IndiGo", False),
    ("infosys", "Infosys", True),
    ("ing", "ING", False),
    ("innovaccer", "Innovaccer", False),
    ("insightsoftware", "insightsoftware", False),
    ("intuit", "Intuit", False),
    ("invesco", "Invesco", False),
    ("irissoftware", "Iris Software", False),
    ("itc", "ITC Limited", False),
    ("ixigo", "ixigo", False),
    ("jioplatforms", "Jio Platforms", False),
    ("jpmorgan", "JPMorgan Chase", False),
    ("jsw", "JSW Group", False),
    ("juspay", "Juspay", False),
    ("kotakbank", "Kotak Mahindra Bank", False),
    ("kpit", "KPIT Technologies", False),
    ("kpmgglobal", "KPMG Global Services", False),
    ("kyndryl", "Kyndryl", False),
    ("larsentoubro", "Larsen & Toubro", False),
    ("lenskart", "Lenskart", False),
    ("lloyds", "Lloyds Banking Group", True),
    ("lowes", "Lowe's", True),
    ("lseg", "LSEG", False),
    ("ltimindtree", "LTIMindtree", True),
    ("ltts", "LTTS", False),
    ("lufthansa", "Lufthansa Group", False),
    ("luxoft", "Luxoft", False),
    ("m2p", "M2P Fintech", False),
    ("macquarie", "Macquarie", False),
    ("maersk", "Maersk", False),
    ("mahindra", "Mahindra & Mahindra", False),
    ("makemytrip", "MakeMyTrip", False),
    ("marshmclennan", "Marsh McLennan", False),
    ("mastek", "Mastek", True),
    ("mastercard", "Mastercard", False),
    ("meesho", "Meesho", False),
    ("meta", "Meta", False),
    ("metlife", "MetLife", False),
    ("micron", "Micron", True),
    ("moengage", "MoEngage", False),
    ("mongodb", "MongoDB", False),
    ("moodys", "Moody's", False),
    ("morganstanley", "Morgan Stanley", False),
    ("morningstar", "Morningstar", False),
    ("mphasis", "Mphasis", True),
    ("msci", "MSCI", False),
    ("mufg", "MUFG", False),
    ("nagarro", "Nagarro", False),
    ("nasdaq", "Nasdaq", False),
    ("natwest", "NatWest Group", False),
    ("necsws", "NEC Software Solutions", False),
    ("netflix", "Netflix", False),
    ("nomura", "Nomura", False),
    ("northerntrust", "Northern Trust", False),
    ("novartis", "Novartis", False),
    ("nutanix", "Nutanix", False),
    ("nvidia", "Nvidia", False),
    ("nykaa", "Nykaa", False),
    ("omnissa", "Omnissa", False),
    ("optum", "Optum", False),
    ("oracle", "Oracle", True),
    ("payoneer", "Payoneer", False),
    ("paypal", "PayPal", False),
    ("paytm", "Paytm", False),
    ("pepsico", "PepsiCo India GCC", False),
    ("perfios", "Perfios", False),
    ("persistent", "Persistent Systems", True),
    ("pfizer", "Pfizer", False),
    ("phonepe", "PhonePe", False),
    ("policybazaar", "PolicyBazaar", False),
    ("postman", "Postman", False),
    ("publicissapient", "Publicis Sapient", False),
    ("pwcac", "PwC Acceleration Centers", False),
    ("qualcomm", "Qualcomm", False),
    ("razorpay", "Razorpay", False),
    ("reliance", "Reliance Industries", False),
    ("resideo", "Resideo", False),
    ("rippling", "Rippling", False),
    ("sabre", "Sabre", True),
    ("salesforce", "Salesforce", False),
    ("saplabs", "SAP Labs", True),
    ("saxobank", "Saxo Bank", False),
    ("schneiderelectric", "Schneider Electric", False),
    ("schwab", "Charles Schwab", True),
    ("servicenow", "ServiceNow", False),
    ("sharechat", "ShareChat", False),
    ("shell", "Shell", False),
    ("siemens", "Siemens", False),
    ("signzy", "Signzy", False),
    ("simcorp", "SimCorp", False),
    ("sita", "SITA", False),
    ("snowflake", "Snowflake", False),
    ("societegenerale", "Societe Generale", True),
    ("sonatasoftware", "Sonata Software", False),
    ("soprasteria", "Sopra Steria", False),
    ("spglobal", "S&P Global", False),
    ("spglobal_careers", "S&P Global Careers", False),
    ("ssc", "SS&C Technologies", False),
    ("standardchartered", "Standard Chartered", False),
    ("statestreet", "State Street", True),
    ("stripe", "Stripe", False),
    ("swiggy", "Swiggy", False),
    ("swissre", "Swiss Re", True),
    ("synchrony", "Synchrony", False),
    ("target", "Target", False),
    ("tcs", "TCS", True),
    ("techmahindra", "Tech Mahindra", True),
    ("thomsonreuters", "Thomson Reuters", False),
    ("thoughtworks", "ThoughtWorks", False),
    ("uber", "Uber", False),
    ("ubs", "UBS", False),
    ("uipath", "UiPath", False),
    ("unitedairlines", "United Airlines", False),
    ("vedanta", "Vedanta", False),
    ("verisk", "Verisk Analytics", False),
    ("verizon", "Verizon", False),
    ("virtusa", "Virtusa", True),
    ("visa", "Visa", False),
    ("walmart", "Walmart Global Tech", False),
    ("wellsfargo", "Wells Fargo", True),
    ("whatfix", "Whatfix", False),
    ("wipro", "Wipro", True),
    ("workday", "Workday", False),
    ("wtw", "WTW", False),
    ("xoriant", "Xoriant", False),
    ("yash", "YASH Technologies", False),
    ("yubi", "Yubi", False),
    ("zensar", "Zensar Technologies", False),
    ("zerodha", "Zerodha", False),
    ("zeta", "Zeta", False),
    ("zoho", "Zoho Corporation", False),
    ("zomato", "Zomato", False),
    ("zurich", "Zurich Insurance", False),
)

_IGNORES_KEYWORDS = frozenset(
    {
        "abinbev", "airindia", "airtel", "akasaair", "allianztech",
        "alphasense",
        "amdocs", "anz",
        "appliedsystems", "arcesium",
        "atlassian",
        "bankofamerica", "birlasoft", "bnpparibas", "boeing", "clearwater", "clevertap",
        "cloudflare", "coforge", "cognizant",
        "cred", "crisil", "cyient",
        "darwinbox", "databricks", "datarobot", "dazn", "deloitteusi",
        "delta", "deltatre", "deutsche", "disney", "dover",
        "energyexemplar", "eygds", "flipkart", "gitlab", "glean", "globallogic", "globant",
        "groww", "happiestminds", "hcltech",
        "honeywell", "ice", "indigo", "innovaccer",
        "infosys", "irissoftware", "itc", "jioplatforms", "jsw", "juspay",
        "kpit", "lenskart",
        "lufthansa", "m2p",
        "maersk", "makemytrip", "mastek", "meesho", "moengage", "mongodb",
        "meta", "metlife", "morningstar", "msci", "natwest", "nomura",
        "nykaa", "omnissa", "payoneer", "paytm", "perfios",
        "persistent", "policybazaar", "postman", "publicissapient",
        "qualcomm", "razorpay", "reliance", "resideo", "rippling",
        "salesforce", "saxobank",
        "schwab",
        "servicenow", "snowflake", "stripe",
        "sharechat", "signzy", "simcorp", "sita",
        "societegenerale", "sonatasoftware", "standardchartered", "swiggy", "swissre",
        "techmahindra", "thoughtworks", "uber", "ubs", "uipath", "vedanta",
        "whatfix", "wipro", "xoriant", "yash", "yubi", "zerodha", "zeta",
        "zoho", "zomato",
    }
)
_SUPPORTS_LOCATION = frozenset(
    {"amdocs", "aon", "apple", "gallagher", "google", "hsbc", "luxoft",
     "morganstanley", "netflix", "pepsico", "publicissapient", "qualcomm",
     "schneiderelectric", "servicenow", "siemens", "spglobal_careers",
     "visa", "zurich"}
)
_INLINE_DESCRIPTIONS = frozenset(
    {
        "abinbev", "airtel", "akasaair", "amazon", "aon", "arcesium",
        "atlassian", "clevertap",
        "cloudflare", "cognizant", "cred",
        "databricks", "dazn", "definitivehealthcare", "energyexemplar",
        "epam", "gallagher",
        "gitlab", "glean", "globant", "google", "groww", "healthedge", "ice",
        "indigo", "juspay",
        "kpit", "lenskart", "m2p", "moengage", "mongodb",
        "meesho", "morningstar", "msci", "payoneer", "paytm", "pepsico", "salesforce",
        "schneiderelectric",
        "sita",
        "uipath", "signzy", "snowflake", "stripe",
        "policybazaar", "postman", "publicissapient", "razorpay", "sharechat",
        "spglobal_careers",
        "swiggy", "ubs", "whatfix", "xoriant", "yubi", "zerodha", "zeta",
    }
)
_NEWEST_FIRST = frozenset({"amazon", "amdocs", "natwest", "optum", "virtusa"})

# Fetchers that drive headless Firefox via Playwright's sync API. A
# ThreadPoolExecutor worker thread that runs one of these leaves an asyncio
# event loop permanently bound to it (Playwright's sync API never tears this
# down mid-process — these modules keep their browser alive as a
# process-lifetime singleton, closed only via atexit). If that same OS
# thread is later reused by the pool for a *different* Playwright-based
# company, the second one fails with "Playwright Sync API inside the
# asyncio loop" — confirmed live in production logs (every scheduled run
# since at least 2026-08-28 lost 7-8 of these companies to this collision).
# run_all.py uses this flag to give each one a dedicated thread instead of
# sharing the general pool, so no OS thread ever runs two of them.
_USES_PLAYWRIGHT = frozenset(
    {"bnpparibas", "darwinbox", "globallogic", "honeywell", "ibm", "indigo",
     "natwest", "perfios", "servicenow", "sonatasoftware", "techmahindra",
     "uber", "virtusa"}
)


def _build_registry() -> dict[str, CompanyPipeline]:
    result = {
        "microsoft": CompanyPipeline(
            slug="microsoft",
            source="Microsoft",
            fetcher_module="fetcher",
            config_key="search",
            seen_file="seen_jobs.json",
            supports_location_filter=True,
            newest_first=True,
        )
    }
    for slug, source, strict in _PIPELINE_DATA:
        result[slug] = CompanyPipeline(
            slug=slug,
            source=source,
            fetcher_module=f"{slug}_fetcher",
            config_key=f"{slug}_search",
            seen_file=f"seen_jobs_{slug}.json",
            description_filter=(
                "require_any_configured_term" if strict else "none"
            ),
            supports_keyword_filter=slug not in _IGNORES_KEYWORDS,
            supports_location_filter=slug in _SUPPORTS_LOCATION,
            description_inline=slug in _INLINE_DESCRIPTIONS,
            newest_first=slug in _NEWEST_FIRST,
            uses_playwright=slug in _USES_PLAYWRIGHT,
        )
    return result


COMPANY_REGISTRY = MappingProxyType(_build_registry())


def get_company(slug: str) -> CompanyPipeline:
    """Return one pipeline definition with a useful error for CLI callers."""
    try:
        return COMPANY_REGISTRY[slug]
    except KeyError as exc:
        choices = ", ".join(COMPANY_REGISTRY)
        raise KeyError(f"unknown company slug {slug!r}; choose one of: {choices}") from exc
