"""
Barclays job-watcher pipeline entry point.

Runs independently of all other company pipelines:
  - Uses barclays_fetcher instead of other company fetchers
  - Writes to seen_jobs_barclays.json instead of seen_jobs.json

Run:  py src/run_barclays.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import barclays_fetcher as _barclays_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_barclays.json"


def run_barclays_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    barclays_cfg = {
        "search": whole_cfg["barclays_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(barclays_cfg, _barclays_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Barclays")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Barclays] Fetched:  {total_fetched} jobs from Barclays careers")
    print(f"[Barclays] Matched:  {len(matched)} passed all filters")
    print(f"[Barclays] New:      {len(new_matches)} not seen before")
    print(f"[Barclays] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_barclays_pipeline()
    except Exception as exc:
        print(f"[Barclays] PIPELINE ERROR: {exc}")
        print("[Barclays] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Barclays", exc)
