"""
Honeywell job-watcher pipeline entry point.

Runs independently of the Microsoft, Optum, Amazon, and Siemens pipelines:
  - Uses honeywell_fetcher instead of other fetchers
  - Writes to seen_jobs_honeywell.json

Run:  py src/run_honeywell.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Honeywell job titles may contain Unicode characters that Windows' cp1252
# console can't encode. Reconfigure stdout to UTF-8 before any print happens.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import honeywell_fetcher as _honeywell_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_honeywell.json"


def run_honeywell_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    honeywell_cfg = {
        "search": whole_cfg["honeywell_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(honeywell_cfg, _honeywell_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Honeywell")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Honeywell] Fetched:  {total_fetched} jobs from Honeywell careers")
    print(f"[Honeywell] Matched:  {len(matched)} passed all filters")
    print(f"[Honeywell] New:      {len(new_matches)} not seen before")
    print(f"[Honeywell] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_honeywell_pipeline()
    except Exception as exc:
        print(f"[Honeywell] PIPELINE ERROR: {exc}")
        print("[Honeywell] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Honeywell", exc)
