"""
Chubb insurance job-watcher pipeline entry point.

Run:  py src/run_chubb.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import chubb_fetcher as _chubb_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_chubb.json"


def run_chubb_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    chubb_cfg = {
        "search": whole_cfg["chubb_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(chubb_cfg, _chubb_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Chubb")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Chubb] Fetched:  {total_fetched} jobs from Chubb careers")
    print(f"[Chubb] Matched:  {len(matched)} passed all filters")
    print(f"[Chubb] New:      {len(new_matches)} not seen before")
    print(f"[Chubb] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_chubb_pipeline()
        reset_failure_count("Chubb")
    except Exception as exc:
        print(f"[Chubb] PIPELINE ERROR: {exc}")
        print("[Chubb] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Chubb", exc)
