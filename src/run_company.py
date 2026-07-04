"""Registry-driven runner for any company pipeline."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

from company_registry import COMPANY_REGISTRY, CompanyPipeline, get_company
from main import load_seen_ids, save_seen_ids
from matcher import _normalize_text, find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"


def _load_fetcher(spec: CompanyPipeline) -> ModuleType:
    module = importlib.import_module(spec.fetcher_module)
    missing = [
        name
        for name in ("fetch_jobs", "fetch_job_description", "RateLimitError")
        if not hasattr(module, name)
    ]
    if missing:
        raise TypeError(
            f"{spec.fetcher_module} does not satisfy the fetcher contract; "
            f"missing: {', '.join(missing)}"
        )
    if not callable(module.fetch_jobs) or not callable(module.fetch_job_description):
        raise TypeError(f"{spec.fetcher_module} fetch functions must be callable")
    if not isinstance(module.RateLimitError, type) or not issubclass(
        module.RateLimitError, Exception
    ):
        raise TypeError(f"{spec.fetcher_module}.RateLimitError must be an exception type")
    return module


def _pipeline_config(whole_cfg: dict[str, Any], spec: CompanyPipeline) -> dict[str, Any]:
    if spec.config_key not in whole_cfg:
        raise KeyError(f"missing required config section: {spec.config_key}")
    configured_search = whole_cfg[spec.config_key]
    if not isinstance(configured_search, dict):
        raise TypeError(f"config section {spec.config_key} must be a mapping")
    # Work on a copy: capability optimizations must never rewrite loaded config.
    search = dict(configured_search)
    keywords = search.get("keywords")
    keywords_valid = (
        isinstance(keywords, list)
        and bool(keywords)
        and all(isinstance(item, str) for item in keywords)
        and (
            not spec.supports_keyword_filter
            or all(item.strip() for item in keywords)
        )
    )
    if not keywords_valid:
        raise ValueError(f"{spec.config_key}.keywords must be a non-empty list of strings")
    if not spec.supports_keyword_filter:
        # These APIs return the same listing set regardless of the query. One
        # pass avoids fetching and parsing identical pages for every keyword.
        search["keywords"] = keywords[:1]
    matching = whole_cfg.get("matching", {})
    if not isinstance(matching, dict):
        raise TypeError("matching config must be a mapping")
    if spec.requires_tech_in_description:
        terms = search.get("require_tech_in_description", [])
        if terms and (
            not isinstance(terms, list)
            or not all(isinstance(item, str) and item.strip() for item in terms)
        ):
            raise ValueError(
                f"{spec.config_key}.require_tech_in_description must be a list of strings"
            )
    return {"search": search, "matching": matching}


def _apply_description_filter(
    jobs: list[dict], search_cfg: dict[str, Any], spec: CompanyPipeline
) -> list[dict]:
    if not spec.requires_tech_in_description:
        return jobs
    terms = search_cfg.get("require_tech_in_description", [])
    if not terms:
        return jobs
    normalized_terms = [_normalize_text(term) for term in terms]
    passed = []
    dropped = []
    for job in jobs:
        description = _normalize_text(job.get("description", ""))
        if any(term in description for term in normalized_terms):
            passed.append(job)
        else:
            dropped.append(job.get("title", "<untitled>"))
    if dropped:
        print(f"{spec.source} description-tech filtered out (near-misses):")
        for title in dropped:
            print(f"  [desc-tech]     {title}")
    return passed


def _delivery_succeeded(result: Any) -> bool:
    """Adapt legacy and structured notifier return values in one small seam.

    The historical notifier returned ``None`` after attempting configured
    channels, so ``None`` remains success for backwards compatibility.  Newer
    notifiers may return a boolean, mapping, or object exposing a success flag.
    """
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for key in (
            "should_mark_seen",
            "delivered",
            "success",
            "any_succeeded",
            "alert_sent",
        ):
            if key in result:
                return bool(result[key])
        channels = result.get("channels")
        if isinstance(channels, dict):
            return any(bool(value) for value in channels.values())
        return False
    for attr in (
        "should_mark_seen",
        "delivered",
        "success",
        "any_succeeded",
        "alert_sent",
    ):
        if hasattr(result, attr):
            return bool(getattr(result, attr))
    return False


def run_company_pipeline(
    slug: str,
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path | None = None,
    *,
    fetcher: ModuleType | None = None,
    notify_func: Callable[..., Any] = notify,
) -> dict[str, Any]:
    """Run one registered company and return machine-readable counters."""
    spec = get_company(slug)
    state_path = seen_path or (_ROOT / spec.seen_file)
    whole_cfg = load_config(config_path)
    pipeline_cfg = _pipeline_config(whole_cfg, spec)
    fetcher = fetcher or _load_fetcher(spec)
    seen_ids = load_seen_ids(state_path)

    total_fetched, matched = find_matching_jobs(
        pipeline_cfg, fetcher, known_ids=seen_ids
    )
    matched = _apply_description_filter(matched, pipeline_cfg["search"], spec)
    new_matches = [job for job in matched if job["id"] not in seen_ids]

    alert_sent = False
    delivery_failed = False
    if new_matches:
        delivery_result = notify_func(new_matches, source=spec.source)
        should_advance_state = _delivery_succeeded(delivery_result)
        if hasattr(delivery_result, "any_succeeded"):
            alert_sent = bool(delivery_result.any_succeeded)
            delivery_failed = bool(delivery_result.any_attempted) and not alert_sent
        else:
            # Legacy notifier/test-double returns treat successful completion as
            # both delivery and permission to advance state.
            alert_sent = should_advance_state
            delivery_failed = not should_advance_state
        if should_advance_state:
            seen_ids.update(job["id"] for job in new_matches)
            save_seen_ids(state_path, seen_ids)
        else:
            print(f"[{spec.source}] No alert channel succeeded; state was not advanced.")

    print(f"[{spec.source}] Fetched:  {total_fetched} jobs")
    print(f"[{spec.source}] Matched:  {len(matched)} passed all filters")
    print(f"[{spec.source}] New:      {len(new_matches)} not seen before")
    alert_status = "sent" if alert_sent else "not sent"
    if not new_matches:
        alert_status += " (no new matches)"
    print(f"[{spec.source}] Alert:    {alert_status}")

    return {
        "slug": slug,
        "source": spec.source,
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
        "delivery_failed": delivery_failed,
        "state_updated": bool(new_matches and not delivery_failed),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", choices=COMPANY_REGISTRY)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--seen", type=Path)
    args = parser.parse_args(argv)
    spec = get_company(args.slug)
    try:
        result = run_company_pipeline(args.slug, args.config, args.seen)
    except Exception as exc:
        print(f"[{spec.source}] PIPELINE ERROR: {exc}", file=sys.stderr)
        notify_pipeline_error(spec.source, exc)
        return 1
    if result["delivery_failed"]:
        exc = RuntimeError("all configured alert channels failed")
        notify_pipeline_error(spec.source, exc)
        return 1
    reset_failure_count(spec.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
