"""Shared serialization lock for Playwright sync-API browser startup.

Each Playwright-based fetcher (see company_registry.py's `uses_playwright`
flag) now runs on its own dedicated thread (run_all.py's `run_companies`),
which fixes the *thread-reuse* class of "Playwright Sync API inside the
asyncio loop" failure. That alone was not enough on GitHub Actions' shared,
CPU-constrained runners: confirmed live (2026-09-01, run 33476619607) that
several of these fetchers still failed with the identical error even on
guaranteed-fresh, never-reused threads, because their `sync_playwright()
.start()` calls landed at nearly the same instant -- a race in Playwright's
own startup bookkeeping that a beefier local machine doesn't reproduce but a
2-core CI runner does reliably.

All Playwright-based fetchers must acquire this lock around their one-time
`sync_playwright().start()` + `.launch(...)` call. The lock is held only for
that brief startup moment -- released immediately after -- so the actual
per-company scraping work (page navigation, extraction) that follows still
runs fully concurrently across companies.
"""
from __future__ import annotations

import threading

STARTUP_LOCK = threading.Lock()
