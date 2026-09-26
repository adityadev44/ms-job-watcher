"""Fetches Dunzo job listings — graceful empty stub.

Company: Dunzo Digital Pvt Ltd ("Dunzo"), a Bengaluru-based quick-commerce /
hyperlocal delivery startup. Founded 2015, once Google-backed and operating
across Bengaluru, Delhi NCR, Hyderabad, Mumbai, Pune, Chennai, and Jaipur.

Status (2026-09-26): Dunzo has effectively wound down. Their domain dunzo.com
is completely unreachable — direct TCP connections are refused (curl exit
code 7, no HTTP response at all). This is consistent with widely-reported
events across 2024–2025:

  - Multiple rounds of mass layoffs (~75 % of workforce let go in two rounds)
  - Several months of unpaid salaries reported by employees (Glassdoor/LinkedIn)
  - Google (the key investor behind the 2022 $240 M round) wrote off its stake
  - Rapid shutdown of operations across most cities
  - The engineering team, once ~200 people, was substantially disbanded
  - No public announcement of closure, but no evidence of active hiring either

ATS investigation: the domain dunzo.com no longer serves any content. A bare
TCP connect to port 443 is refused, so no ATS discovery was possible. Their
last-known public careers path was https://www.dunzo.com/careers — the domain
does not resolve to a live server as of the date above.

This fetcher is a registered no-op: every call returns empty results and logs
a single informational message. No exceptions are ever raised. The pipeline
entry, seen-jobs file, and config section are kept so that:
  1. The run loop does not error out on a missing slug.
  2. If Dunzo relaunches under this or a successor domain, the fetcher can be
     updated in one place without touching the registry/config/workflow.

India offices: Bengaluru (only, when operational).
"""
from __future__ import annotations


class RateLimitError(Exception):
    """Raised on persistent fetch failure — never raised by this stub."""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return an empty list.

    Dunzo's website is unreachable (company appears to have wound down as of
    2026). Keyword, location, and pagination arguments are accepted for
    interface compatibility but are unused.
    """
    print(
        "[Dunzo] Careers site unreachable — company appears to have wound down; "
        "returning 0 jobs."
    )
    return []


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return empty (description, posting_date) — Dunzo has no active listings."""
    return "", ""
