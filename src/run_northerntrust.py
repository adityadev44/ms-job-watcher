"""
Northern Trust job-watcher pipeline entry point.

Runs independently of all other company pipelines:
  - Uses northerntrust_fetcher instead of fetcher / siemens_fetcher / etc.
  - Writes to seen_jobs_northerntrust.json instead of seen_jobs.json

Run:  py src/run_northerntrust.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import northerntrust_fetcher as _northerntrust_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_northerntrust.json"


def run_northerntrust_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    northerntrust_cfg = {
        "search": whole_cfg["northerntrust_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(northerntrust_cfg, _northerntrust_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Northern Trust")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Northern Trust] Fetched:  {total_fetched} jobs from Northern Trust careers")
    print(f"[Northern Trust] Matched:  {len(matched)} passed all filters")
    print(f"[Northern Trust] New:      {len(new_matches)} not seen before")
    print(f"[Northern Trust] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_northerntrust_pipeline()
    except Exception as exc:
        print(f"[Northern Trust] PIPELINE ERROR: {exc}")
        print("[Northern Trust] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Northern Trust", exc)
