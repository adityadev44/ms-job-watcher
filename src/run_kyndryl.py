"""
Kyndryl job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses kyndryl_fetcher instead of other fetchers
  - Writes to seen_jobs_kyndryl.json instead of seen_jobs.json

Run:  py src/run_kyndryl.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import kyndryl_fetcher as _kyndryl_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_kyndryl.json"


def run_kyndryl_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    kyndryl_cfg = {
        "search": whole_cfg["kyndryl_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(kyndryl_cfg, _kyndryl_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Kyndryl")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Kyndryl] Fetched:  {total_fetched} jobs from Kyndryl careers")
    print(f"[Kyndryl] Matched:  {len(matched)} passed all filters")
    print(f"[Kyndryl] New:      {len(new_matches)} not seen before")
    print(f"[Kyndryl] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_kyndryl_pipeline()
        reset_failure_count("Kyndryl")
    except Exception as exc:
        print(f"[Kyndryl] PIPELINE ERROR: {exc}")
        print(f"[Kyndryl] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Kyndryl", exc)
