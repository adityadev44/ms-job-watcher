"""
S&P Global job-watcher pipeline entry point.

Run:  py src/run_spglobal.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import spglobal_fetcher as _spg_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_spglobal.json"


def run_spglobal_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    spg_cfg = {
        "search": whole_cfg["spglobal_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(spg_cfg, _spg_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="S&P Global")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[S&P Global] Fetched:  {total_fetched} jobs from S&P Global careers")
    print(f"[S&P Global] Matched:  {len(matched)} passed all filters")
    print(f"[S&P Global] New:      {len(new_matches)} not seen before")
    print(f"[S&P Global] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_spglobal_pipeline()
        reset_failure_count("S&P Global")
    except Exception as exc:
        print(f"[S&P Global] PIPELINE ERROR: {exc}")
        print("[S&P Global] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("S&P Global", exc)
