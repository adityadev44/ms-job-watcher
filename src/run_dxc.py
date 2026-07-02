"""
DXC Technology job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses dxc_fetcher instead of other fetchers
  - Writes to seen_jobs_dxc.json instead of seen_jobs.json

Run:  py src/run_dxc.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import dxc_fetcher as _dxc_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_dxc.json"


def run_dxc_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    dxc_cfg = {
        "search": whole_cfg["dxc_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(dxc_cfg, _dxc_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="DXC Technology")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[DXC Technology] Fetched:  {total_fetched} jobs from DXC Technology careers")
    print(f"[DXC Technology] Matched:  {len(matched)} passed all filters")
    print(f"[DXC Technology] New:      {len(new_matches)} not seen before")
    print(f"[DXC Technology] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_dxc_pipeline()
        reset_failure_count("DXC Technology")
    except Exception as exc:
        print(f"[DXC Technology] PIPELINE ERROR: {exc}")
        print(f"[DXC Technology] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("DXC Technology", exc)
