"""Run registered company watchers with bounded concurrency."""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import run_company
from company_registry import COMPANY_REGISTRY

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_WORKERS = 10


def _worker_default() -> int:
    raw = os.getenv("JOB_WATCHER_WORKERS", str(_DEFAULT_WORKERS))
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_WORKERS
    return value if value > 0 else _DEFAULT_WORKERS


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _selected_companies(groups: list[list[str]] | None) -> list[str]:
    if not groups:
        return sorted(COMPANY_REGISTRY)
    requested = []
    for group in groups:
        for value in group:
            requested.extend(part.strip() for part in value.split(",") if part.strip())
    unknown = sorted(set(requested) - COMPANY_REGISTRY.keys())
    if unknown:
        raise ValueError(f"unknown companies: {', '.join(unknown)}")
    # Stable order and de-duplication make both execution logs and summaries predictable.
    return sorted(set(requested))


def validate_companies(companies: list[str], config_path: Path) -> dict[str, str]:
    """Validate config sections and fetcher contracts without making web requests."""
    errors: dict[str, str] = {}
    try:
        whole_config = run_company.load_config(config_path)
    except Exception as exc:
        return {slug: f"config could not be loaded: {exc}" for slug in companies}

    for slug in companies:
        spec = COMPANY_REGISTRY[slug]
        try:
            run_company._pipeline_config(whole_config, spec)
            run_company._load_fetcher(spec)
        except Exception as exc:
            errors[slug] = str(exc)
    return errors


def run_companies(
    companies: list[str], config_path: Path, max_workers: int
) -> dict[str, str]:
    """Run every selected pipeline, allowing failures without cancelling peers."""
    statuses: dict[str, str] = {}

    def invoke(slug: str) -> int:
        return run_company.main([slug, "--config", str(config_path)])

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="watcher") as pool:
        future_to_slug = {pool.submit(invoke, slug): slug for slug in companies}
        for future in as_completed(future_to_slug):
            slug = future_to_slug[future]
            try:
                statuses[slug] = "ok" if future.result() == 0 else "failed"
            except Exception as exc:
                # The generic runner normally contains pipeline errors. This guard ensures
                # an orchestration bug still cannot prevent other futures from finishing.
                print(f"[{slug}] LAUNCHER ERROR: {exc}", file=sys.stderr)
                statuses[slug] = "failed"
    return statuses


def _print_summary(statuses: dict[str, str], *, validation: bool = False) -> None:
    label = "VALIDATION SUMMARY" if validation else "PIPELINE SUMMARY"
    print(f"\n=== {label} ===")
    for slug in sorted(statuses):
        print(f"{slug}: {statuses[slug]}")
    failed = sum(status != "ok" for status in statuses.values())
    print(f"Total: {len(statuses)} | Succeeded: {len(statuses) - failed} | Failed: {failed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--companies",
        action="append",
        nargs="+",
        metavar="SLUG",
        help="run a subset (space- or comma-separated); may be repeated",
    )
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=_worker_default(),
        help="maximum concurrent pipelines (default: %(default)s; env: JOB_WATCHER_WORKERS)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="validate registry, config, and fetcher contracts without running watchers",
    )
    args = parser.parse_args(argv)

    try:
        companies = _selected_companies(args.companies)
    except ValueError as exc:
        parser.error(str(exc))

    if args.validate:
        errors = validate_companies(companies, args.config)
        statuses = {slug: errors.get(slug, "ok") for slug in companies}
        _print_summary(statuses, validation=True)
        return 1 if errors else 0

    statuses = run_companies(companies, args.config, args.workers)
    _print_summary(statuses)
    return 1 if any(status != "ok" for status in statuses.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
