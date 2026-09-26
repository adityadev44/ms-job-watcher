"""Dream11 job fetcher — no confirmed public ATS found (graceful stub).

Dream11 (Sporta Technologies Private Limited) is India's largest fantasy sports
platform, headquartered in Mumbai with engineering in Mumbai, Pune, and
Bengaluru.

ATS investigation (2026-09-26):
    - www.dream11.com/careers: page loads but only shows a footer link, no
      embedded ATS iframe or redirect to a job board.
    - LinkedIn (linkedin.com/company/dream11/jobs/): 0 current open jobs.
    - Lever: tokens "dream11", "dream11sports", "sportatechnologies" all
      return HTTP 404 — no Lever board.
    - Greenhouse: tokens "dream11", "dream11sports",
      "dream11sportsprivatelimited", "sportatechnologies",
      "sportatechnologiesprivatelimited" all return HTTP 404.
    - SmartRecruiters: careers.smartrecruiters.com/Dream11 and /Dream111
      redirect to the generic SmartRecruiters homepage (302 → jobs.smartrecruiters.com)
      — not a registered tenant. API endpoint returns 0 with no company object.
    - Keka: dream11.keka.com/careers → 404.
    - Darwinbox: dream11.darwinbox.in → blank page; sporta.darwinbox.com →
      blank page.
    - Zoho Recruit: dream11.zohorecruit.com → "site does not exist".

Conclusion: Dream11 either manages hiring through LinkedIn/internal portals
only, or has a private ATS not accessible without authentication. As of
2026-09-26 there are also genuinely 0 open jobs visible anywhere. This
fetcher is a graceful stub that returns empty results rather than crashing
the pipeline. When Dream11 re-opens public hiring, re-investigate the
careers page to identify the new ATS and update this fetcher.
"""
from __future__ import annotations


class RateLimitError(Exception):
    """Raised on 429 or persistent network failure (unused in stub)."""


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return an empty list — no public ATS found for Dream11.

    All parameters are accepted for interface compatibility with the generic
    runner but are unused. Returns [] so the pipeline runs without error.
    """
    return []


def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    """Return ('', '') — stub for interface compatibility.

    Since fetch_jobs() always returns [] this function is never called in
    normal operation. It exists to satisfy the fetcher contract.
    """
    return "", ""
