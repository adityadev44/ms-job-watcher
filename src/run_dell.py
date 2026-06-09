"""
Dell Technologies job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses dell_fetcher (Oracle HCM Cloud REST API, plain requests)
  - Writes to seen_jobs_dell.json

Run:  py src/run_dell.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import dell_fetcher as _dell_mod
from matcher import find_matching_jobs, load_config
from notifier import notify
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_dell.json"


def run_dell_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    dell_cfg = {
        "search": whole_cfg["dell_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(dell_cfg, _dell_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Dell Technologies")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Dell] Fetched:  {total_fetched} jobs from Dell Technologies careers")
    print(f"[Dell] Matched:  {len(matched)} passed all filters")
    print(f"[Dell] New:      {len(new_matches)} not seen before")
    print(f"[Dell] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_dell_pipeline()
    except Exception as exc:
        print(f"[Dell] PIPELINE ERROR: {exc}")
        print("[Dell] Exiting cleanly to avoid blocking other pipelines.")
