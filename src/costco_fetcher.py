"""Costco IT India jobs via Talent500 (ANSR managed GCC platform).

Costco Wholesale opened a Hyderabad GCC in July 2025 with 1,000 planned
hires. India roles include .NET/C#, Python, AI/ML, and data engineering.

All India GCC jobs are posted exclusively via Talent500:
    talent500.com/jobs/costco/
    company_slug: "costco"

The Talent500 backend REST API (prod-warmachine.talent500.co) is public
and requires no authentication -- confirmed live 2026-10-03. 29 total
jobs as of check; all in Hyderabad. Confirmed tech roles:
- Software Engineer 3 - C# / .Net Core
- Senior Software Engineer - C# / .Net Core
- Software Engineer AI - Computer Vision
- Software Engineer - MLOps
- Software Engineer - AI - Innovation Engineering
- Data Scientist 3/4 - Vertex AI
- Data Engineer (Level 2b, Level 3, BigQuery/Dataflows)

No server-side keyword filter (board fetched in full per run).
Descriptions are inline (primary_skills + secondary_skills joined as text).
require_tech_in_description=True filters out non-tech roles (HR Manager,
Procurement Manager, Payroll Specialist, etc.).

See _talent500_common.py for shared fetch logic.
"""
from __future__ import annotations

from _talent500_common import RateLimitError, fetch_jobs_for_company

_COMPANY_SLUG = "costco"


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
