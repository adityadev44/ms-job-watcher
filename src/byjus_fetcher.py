"""Fetches BYJU's job listings — graceful empty fetcher.

Company: BYJU's (Think & Learn Pvt Ltd), India's edtech company.
ATS status: DEGRADED / INACCESSIBLE as of 2026-09-26.

Discovery notes (2026-09-26):
  - Lever board "byjus"         → api.lever.co/v0/postings/byjus        → HTTP 404
  - Lever board "thinkandlearn" → api.lever.co/v0/postings/thinkandlearn → HTTP 404
  - Greenhouse board "byjus"    → boards-api.greenhouse.io/v1/boards/byjus/jobs → HTTP 404
  - careers.byjus.com           → SSL certificate error (certificate-not-found.jobs2web.com)
  - byjus.com/careers/          → custom page, no public ATS API endpoint discoverable
  - byjus.com/apply/            → direct apply form, not a structured job board

Context: BYJU's (Think & Learn Pvt Ltd) underwent major financial distress
in 2024-2025, including insolvency proceedings, mass layoffs, and significant
leadership/operational restructuring. Their recruitment infrastructure is
effectively non-functional as of late 2026. The company is registered in this
pipeline in case they resume hiring on a discoverable ATS, at which point this
fetcher should be replaced with a real implementation.

This fetcher returns empty results without crashing, so the pipeline runs
cleanly. No HTTP calls are made; the rate-limit counter is never triggered.

India offices (historical): Bengaluru (HQ), Hyderabad, Delhi.
"""
from __future__ import annotations


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure (unused in this stub)."""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return empty list — BYJU's ATS is currently inaccessible.

    See module docstring for discovery history. When BYJU's resumes hiring
    on a public ATS, replace this stub with a real implementation.
    """
    return []


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return ('', '') — no description available without an accessible board."""
    return "", ""
