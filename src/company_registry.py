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
    ("accenture", "Accenture", True),
    ("adobe", "Adobe", True),
    ("aig", "AIG", False),
    ("alphasense", "AlphaSense", False),
    ("amazon", "Amazon", False),
    ("amdocs", "Amdocs", False),
    ("ameriprise", "Ameriprise", False),
    ("amex", "American Express", False),
    ("anz", "ANZ", False),
    ("arcesium", "Arcesium", False),
    ("atlassian", "Atlassian", False),
    ("autodesk", "Autodesk", True),
    ("automationanywhere", "Automation Anywhere", False),
    ("bankofamerica", "Bank of America", True),
    ("barclays", "Barclays", False),
    ("blackrock", "BlackRock", False),
    ("bloomberg", "Bloomberg", False),
    ("bnpparibas", "BNP Paribas", False),
    ("bny", "BNY Mellon", False),
    ("broadridge", "Broadridge", False),
    ("capgemini", "Capgemini", True),
    ("chubb", "Chubb", False),
    ("citi", "Citi", True),
    ("citiustech", "CitiusTech", True),
    ("cognizant", "Cognizant", True),
    ("cred", "CRED", False),
    ("crisil", "CRISIL", False),
    ("datarobot", "DataRobot", False),
    ("dell", "Dell Technologies", False),
    ("deutsche", "Deutsche Bank", False),
    ("dtcc", "DTCC", False),
    ("dxc", "DXC Technology", True),
    ("ebay", "eBay", True),
    ("eclerx", "eClerx", True),
    ("factset", "FactSet", False),
    ("fidelity", "Fidelity", False),
    ("firstamerican", "First American", True),
    ("fis", "FIS Global", False),
    ("fiserv", "Fiserv", False),
    ("flipkart", "Flipkart", False),
    ("gallagher", "Gallagher", False),
    ("generalmotors", "General Motors", False),
    ("genpact", "Genpact", True),
    ("glean", "Glean", False),
    ("goldmansachs", "Goldman Sachs", False),
    ("google", "Google", False),
    ("groww", "Groww", False),
    ("hcltech", "HCLTech", True),
    ("hexaware", "Hexaware", True),
    ("honeywell", "Honeywell", False),
    ("hsbc", "HSBC", False),
    ("ibm", "IBM", True),
    ("ice", "ICE", False),
    ("icertis", "Icertis", False),
    ("infosys", "Infosys", True),
    ("ing", "ING", False),
    ("intuit", "Intuit", False),
    ("invesco", "Invesco", False),
    ("jioplatforms", "Jio Platforms", False),
    ("jpmorgan", "JPMorgan Chase", False),
    ("juspay", "Juspay", False),
    ("kyndryl", "Kyndryl", False),
    ("lenskart", "Lenskart", False),
    ("lloyds", "Lloyds Banking Group", True),
    ("lowes", "Lowe's", True),
    ("lseg", "LSEG", False),
    ("ltimindtree", "LTIMindtree", True),
    ("m2p", "M2P Fintech", False),
    ("macquarie", "Macquarie", False),
    ("maersk", "Maersk", False),
    ("marshmclennan", "Marsh McLennan", False),
    ("mastek", "Mastek", True),
    ("mastercard", "Mastercard", False),
    ("meesho", "Meesho", False),
    ("meta", "Meta", False),
    ("metlife", "MetLife", False),
    ("micron", "Micron", True),
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
    ("nomura", "Nomura", False),
    ("northerntrust", "Northern Trust", False),
    ("nykaa", "Nykaa", False),
    ("optum", "Optum", False),
    ("oracle", "Oracle", True),
    ("paypal", "PayPal", False),
    ("paytm", "Paytm", False),
    ("perfios", "Perfios", False),
    ("persistent", "Persistent Systems", True),
    ("phonepe", "PhonePe", False),
    ("policybazaar", "PolicyBazaar", False),
    ("razorpay", "Razorpay", False),
    ("sabre", "Sabre", True),
    ("saplabs", "SAP Labs", True),
    ("schwab", "Charles Schwab", True),
    ("servicenow", "ServiceNow", False),
    ("sharechat", "ShareChat", False),
    ("siemens", "Siemens", False),
    ("signzy", "Signzy", False),
    ("societegenerale", "Societe Generale", True),
    ("spglobal", "S&P Global", False),
    ("spglobal_careers", "S&P Global Careers", False),
    ("standardchartered", "Standard Chartered", False),
    ("statestreet", "State Street", True),
    ("swiggy", "Swiggy", False),
    ("swissre", "Swiss Re", True),
    ("synchrony", "Synchrony", False),
    ("target", "Target", False),
    ("tcs", "TCS", True),
    ("techmahindra", "Tech Mahindra", True),
    ("thomsonreuters", "Thomson Reuters", False),
    ("ubs", "UBS", False),
    ("uipath", "UiPath", False),
    ("verizon", "Verizon", False),
    ("virtusa", "Virtusa", True),
    ("visa", "Visa", False),
    ("wellsfargo", "Wells Fargo", True),
    ("wipro", "Wipro", True),
    ("wtw", "WTW", False),
    ("yubi", "Yubi", False),
    ("zerodha", "Zerodha", False),
    ("zeta", "Zeta", False),
    ("zomato", "Zomato", False),
)

_IGNORES_KEYWORDS = frozenset(
    {
        "alphasense", "amdocs", "anz", "arcesium", "atlassian",
        "bankofamerica", "bnpparibas", "cognizant", "cred", "crisil",
        "datarobot", "deutsche", "flipkart", "glean", "groww", "hcltech",
        "honeywell", "ice",
        "infosys", "jioplatforms", "juspay", "lenskart", "m2p", "maersk",
        "mastek", "meesho",
        "meta", "metlife", "morningstar", "msci", "natwest", "nomura",
        "nykaa", "paytm", "perfios",
        "persistent", "policybazaar", "razorpay", "schwab", "servicenow",
        "sharechat", "signzy",
        "societegenerale", "standardchartered", "swiggy", "swissre",
        "techmahindra", "ubs", "uipath", "wipro", "yubi", "zerodha",
        "zeta", "zomato",
    }
)
_SUPPORTS_LOCATION = frozenset(
    {"amdocs", "gallagher", "google", "hsbc", "morganstanley", "servicenow",
     "siemens", "spglobal_careers", "visa"}
)
_INLINE_DESCRIPTIONS = frozenset(
    {
        "amazon", "arcesium", "atlassian", "cognizant", "cred", "gallagher",
        "glean", "google", "groww", "ice", "juspay", "lenskart", "m2p",
        "meesho", "morningstar", "msci", "paytm",
        "uipath", "signzy",
        "policybazaar", "razorpay", "sharechat", "spglobal_careers",
        "swiggy", "ubs", "yubi", "zerodha", "zeta",
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
    {"bnpparibas", "honeywell", "ibm", "natwest", "perfios", "servicenow",
     "techmahindra", "virtusa"}
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
