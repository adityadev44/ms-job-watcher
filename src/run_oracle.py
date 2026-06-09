"""
Oracle / Oracle Health job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses oracle_fetcher (Oracle HCM Cloud REST API, plain requests)
  - Writes to seen_jobs_oracle.json
  - Oracle Health jobs are on the same platform — no separate pipeline needed

Run:  py src/run_oracle.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import oracle_fetcher as _oracle_mod
from matcher import find_matching_jobs, load_config
from notifier import notify
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_oracle.json"


def run_oracle_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    oracle_cfg = {
        "search": whole_cfg["oracle_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(oracle_cfg, _oracle_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Oracle")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Oracle] Fetched:  {total_fetched} jobs from Oracle careers")
    print(f"[Oracle] Matched:  {len(matched)} passed all filters")
    print(f"[Oracle] New:      {len(new_matches)} not seen before")
    print(f"[Oracle] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_oracle_pipeline()
    except Exception as exc:
        print(f"[Oracle] PIPELINE ERROR: {exc}")
        print("[Oracle] Exiting cleanly to avoid blocking other pipelines.")
