"""
Mastercard job-watcher pipeline entry point.

Runs independently of the Microsoft, Optum, and Amazon pipelines:
  - Uses mastercard_fetcher instead of fetcher / optum_fetcher / amazon_fetcher
  - Writes to seen_jobs_mastercard.json instead of seen_jobs.json

Run:  py src/run_mastercard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import mastercard_fetcher as _mastercard_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_mastercard.json"


def run_mastercard_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    mastercard_cfg = {
        "search": whole_cfg["mastercard_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(mastercard_cfg, _mastercard_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Mastercard")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Mastercard] Fetched:  {total_fetched} jobs from Mastercard careers")
    print(f"[Mastercard] Matched:  {len(matched)} passed all filters")
    print(f"[Mastercard] New:      {len(new_matches)} not seen before")
    print(f"[Mastercard] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_mastercard_pipeline()
    except Exception as exc:
        print(f"[Mastercard] PIPELINE ERROR: {exc}")
        print("[Mastercard] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Mastercard", exc)
