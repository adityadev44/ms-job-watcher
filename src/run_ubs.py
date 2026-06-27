"""
UBS job-watcher pipeline entry point.

Runs independently of all other company pipelines:
  - Uses ubs_fetcher (IBM BrassRing at jobs.ubs.com)
  - Writes to seen_jobs_ubs.json

Run:  py src/run_ubs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import ubs_fetcher as _ubs_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_ubs.json"


def run_ubs_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    ubs_cfg = {
        "search": whole_cfg["ubs_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(ubs_cfg, _ubs_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="UBS")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[UBS] Fetched:  {total_fetched} jobs from UBS careers")
    print(f"[UBS] Matched:  {len(matched)} passed all filters")
    print(f"[UBS] New:      {len(new_matches)} not seen before")
    print(f"[UBS] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_ubs_pipeline()
        reset_failure_count("UBS")
    except Exception as exc:
        print(f"[UBS] PIPELINE ERROR: {exc}")
        print("[UBS] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("UBS", exc)
