"""T-Mobile India (TMUS Global Solutions) jobs via Talent500.

T-Mobile opened a Hyderabad GCC (TMUS Global Solutions) in June 2026,
~250,000 sq ft campus, 1,000 planned hires. Roles are software and
platform engineering -- Python backend, AI/ML/LLM, DevOps/SRE.

All India GCC jobs are posted exclusively via Talent500:
    talent500.com/jobs/t-mobile/
    company_slug: "t-mobile"

The Talent500 backend REST API (prod-warmachine.talent500.co) is public
and requires no authentication -- confirmed live 2026-10-03. 73 total
jobs as of check; all in Hyderabad. Confirmed tech roles:
- Sr Engineer AI (MCP/A2A/ACP/LLM protocols)
- Manager, Software Engineering (Agentic AI/Gen AI/LLM)
- Sr Engineer, Software - Python Backend (Python/Redis/Kafka/FastAPI)
- Engineer Software - Platform (Python/IaC/Control-M)
- Sr Architect, Data (Python/Snowflake/Databricks)
- Manager, DevOps (AWS/Azure/CI-CD)
- Principal Engineer, Site Reliability (Java/AWS)

No server-side keyword filter (board fetched in full per run).
Descriptions are inline (primary_skills + secondary_skills joined as text).
require_tech_in_description=True filters out non-tech roles (benefits,
payroll, HR, SOX compliance, etc.).

See _talent500_common.py for shared fetch logic.
"""
from __future__ import annotations

from _talent500_common import RateLimitError, fetch_jobs_for_company

_COMPANY_SLUG = "t-mobile"


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    return fetch_jobs_for_company(_COMPANY_SLUG, num=num, start=start, timeout=timeout)


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    raise NotImplementedError("descriptions are inline")
