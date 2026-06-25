"""
Synchrony Financial job-watcher pipeline entry point.

Runs independently of the Microsoft, Optum, and Amazon pipelines:
  - Uses synchrony_fetcher instead of fetcher / optum_fetcher / amazon_fetcher
  - Writes to seen_jobs_synchrony.json instead of seen_jobs.json

Run:  py src/run_synchrony.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Reconfigure stdout to UTF-8 before any print happens (job titles / descriptions
# can contain non-cp1252 characters on Windows consoles).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import synchrony_fetcher as _synchrony_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_synchrony.json"


def run_synchrony_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    synchrony_cfg = {
        "search": whole_cfg["synchrony_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(synchrony_cfg, _synchrony_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Synchrony")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Synchrony] Fetched:  {total_fetched} jobs from Synchrony careers")
    print(f"[Synchrony] Matched:  {len(matched)} passed all filters")
    print(f"[Synchrony] New:      {len(new_matches)} not seen before")
    print(f"[Synchrony] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_synchrony_pipeline()
    except Exception as exc:
        print(f"[Synchrony] PIPELINE ERROR: {exc}")
        print("[Synchrony] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Synchrony", exc)
