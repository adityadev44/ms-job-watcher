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

    @property
    def requires_tech_in_description(self) -> bool:
        return self.description_filter == "require_any_configured_term"


# slug, alert/display source, strict description filter.  The remaining names
# follow the conventions used by all 73 non-Microsoft adapters.
_PIPELINE_DATA = (
    ("accenture", "Accenture", True),
    ("adobe", "Adobe", True),
    ("amazon", "Amazon", False),
    ("ameriprise", "Ameriprise", False),
    ("amex", "American Express", False),
    ("autodesk", "Autodesk", True),
    ("bankofamerica", "Bank of America", True),
    ("barclays", "Barclays", False),
    ("bny", "BNY Mellon", False),
    ("broadridge", "Broadridge", False),
    ("capgemini", "Capgemini", True),
    ("chubb", "Chubb", False),
    ("citi", "Citi", True),
    ("cognizant", "Cognizant", True),
    ("dell", "Dell Technologies", False),
    ("deutsche", "Deutsche Bank", False),
    ("dxc", "DXC Technology", True),
    ("ebay", "eBay", True),
    ("factset", "FactSet", False),
    ("fidelity", "Fidelity", False),
    ("firstamerican", "First American", True),
    ("fis", "FIS Global", False),
    ("fiserv", "Fiserv", False),
    ("gallagher", "Gallagher", False),
    ("generalmotors", "General Motors", False),
    ("genpact", "Genpact", True),
    ("goldmansachs", "Goldman Sachs", False),
    ("hcltech", "HCLTech", True),
    ("hexaware", "Hexaware", True),
    ("honeywell", "Honeywell", False),
    ("hsbc", "HSBC", False),
    ("ibm", "IBM", True),
    ("icertis", "Icertis", False),
    ("infosys", "Infosys", True),
    ("invesco", "Invesco", False),
    ("jpmorgan", "JPMorgan Chase", False),
    ("kyndryl", "Kyndryl", False),
    ("lowes", "Lowe's", True),
    ("lseg", "LSEG", False),
    ("ltimindtree", "LTIMindtree", True),
    ("maersk", "Maersk", False),
    ("marshmclennan", "Marsh McLennan", False),
    ("mastercard", "Mastercard", False),
    ("metlife", "MetLife", False),
    ("micron", "Micron", True),
    ("morganstanley", "Morgan Stanley", False),
    ("morningstar", "Morningstar", False),
    ("msci", "MSCI", False),
    ("nagarro", "Nagarro", False),
    ("nomura", "Nomura", False),
    ("northerntrust", "Northern Trust", False),
    ("optum", "Optum", False),
    ("oracle", "Oracle", True),
    ("paypal", "PayPal", False),
    ("persistent", "Persistent Systems", True),
    ("sabre", "Sabre", True),
    ("schwab", "Charles Schwab", True),
    ("siemens", "Siemens", False),
    ("societegenerale", "Societe Generale", True),
    ("spglobal", "S&P Global", False),
    ("spglobal_careers", "S&P Global Careers", False),
    ("standardchartered", "Standard Chartered", False),
    ("statestreet", "State Street", True),
    ("synchrony", "Synchrony", False),
    ("target", "Target", False),
    ("tcs", "TCS", True),
    ("techmahindra", "Tech Mahindra", True),
    ("ubs", "UBS", False),
    ("verizon", "Verizon", False),
    ("virtusa", "Virtusa", True),
    ("wellsfargo", "Wells Fargo", True),
    ("wipro", "Wipro", True),
    ("wtw", "WTW", False),
)

_IGNORES_KEYWORDS = frozenset(
    {
        "bankofamerica", "cognizant", "deutsche", "hcltech", "honeywell",
        "infosys", "maersk", "metlife", "morningstar", "msci", "nomura",
        "persistent", "schwab", "societegenerale", "standardchartered",
        "techmahindra", "ubs", "wipro",
    }
)
_SUPPORTS_LOCATION = frozenset(
    {"gallagher", "hsbc", "morganstanley", "siemens", "spglobal_careers"}
)
_INLINE_DESCRIPTIONS = frozenset(
    {"amazon", "cognizant", "gallagher", "morningstar", "msci", "spglobal_careers", "ubs"}
)
_NEWEST_FIRST = frozenset({"amazon", "optum", "virtusa"})


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
