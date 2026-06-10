"""
Optum/UHG job-watcher pipeline entry point.

Runs independently of the Microsoft pipeline (src/main.py):
  - Uses optum_fetcher instead of fetcher
  - Writes to seen_jobs_optum.json instead of seen_jobs.json

Run:  py src/run_optum.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import optum_fetcher as _optum_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_optum.json"


def run_optum_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    optum_cfg = {
        "search": whole_cfg["optum_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(optum_cfg, _optum_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Optum")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Optum] Fetched:  {total_fetched} jobs from UHG careers")
    print(f"[Optum] Matched:  {len(matched)} passed all filters")
    print(f"[Optum] New:      {len(new_matches)} not seen before")
    print(f"[Optum] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_optum_pipeline()
    except Exception as exc:
        print(f"[Optum] PIPELINE ERROR: {exc}")
        print("[Optum] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Optum", exc)
