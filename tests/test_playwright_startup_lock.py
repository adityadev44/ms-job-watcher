"""Every Playwright-based fetcher must serialize its browser startup.

Confirmed live in production (2026-09-01, GitHub Actions run 33476619607):
even with one dedicated thread per Playwright-based company (see
company_registry.py's uses_playwright flag / run_all.py's dedicated pool),
several still failed with "Playwright Sync API inside the asyncio loop"
because their `sync_playwright().start()` calls landed at nearly the same
instant on the CI runner's constrained CPU. Wrapping that one-time startup
in a shared `_playwright_startup.STARTUP_LOCK` fixed it. This test guards
against a future Playwright-based fetcher forgetting the lock.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from company_registry import COMPANY_REGISTRY


def _source(slug: str) -> str:
    spec = COMPANY_REGISTRY[slug]
    return (ROOT / "src" / f"{spec.fetcher_module}.py").read_text(encoding="utf-8")


def test_every_playwright_backed_fetcher_uses_the_shared_startup_lock() -> None:
    playwright_slugs = [s for s, spec in COMPANY_REGISTRY.items() if spec.uses_playwright]
    assert playwright_slugs, "expected at least one Playwright-backed company"

    for slug in playwright_slugs:
        source = _source(slug)
        assert "from _playwright_startup import STARTUP_LOCK" in source, (
            f"{slug}_fetcher.py uses Playwright but never imports STARTUP_LOCK"
        )
        assert "with STARTUP_LOCK:" in source, (
            f"{slug}_fetcher.py imports STARTUP_LOCK but never holds it "
            "around browser startup"
        )
        # The `sync_playwright().start()` call itself must be inside that
        # `with` block, not merely present somewhere else in the module.
        tree = ast.parse(source)
        locked_start_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            holds_lock = any(
                isinstance(item.context_expr, ast.Name) and item.context_expr.id == "STARTUP_LOCK"
                for item in node.items
            )
            if not holds_lock:
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "start"
                    and isinstance(inner.func.value, ast.Call)
                    and isinstance(inner.func.value.func, ast.Name)
                    and inner.func.value.func.id == "sync_playwright"
                ):
                    locked_start_calls += 1
        assert locked_start_calls >= 1, (
            f"{slug}_fetcher.py's sync_playwright().start() call is not "
            "actually inside a `with STARTUP_LOCK:` block"
        )
