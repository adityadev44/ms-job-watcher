"""
Goldman Sachs job-watcher pipeline entry point.

Runs independently of the other pipelines:
  - Uses goldmansachs_fetcher instead of fetcher / optum_fetcher / etc.
  - Writes to seen_jobs_goldmansachs.json instead of seen_jobs.json

Run:  py src/run_goldmansachs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import goldmansachs_fetcher as _goldmansachs_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_goldmansachs.json"


def run_goldmansachs_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    goldmansachs_cfg = {
        "search": whole_cfg["goldmansachs_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(goldmansachs_cfg, _goldmansachs_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Goldman Sachs")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Goldman Sachs] Fetched:  {total_fetched} jobs from Goldman Sachs careers")
    print(f"[Goldman Sachs] Matched:  {len(matched)} passed all filters")
    print(f"[Goldman Sachs] New:      {len(new_matches)} not seen before")
    print(f"[Goldman Sachs] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_goldmansachs_pipeline()
        reset_failure_count("Goldman Sachs")
    except Exception as exc:
        print(f"[Goldman Sachs] PIPELINE ERROR: {exc}")
        print("[Goldman Sachs] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Goldman Sachs", exc)
