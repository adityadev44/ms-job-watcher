"""
WTW (Willis Towers Watson) job-watcher pipeline entry point.

Run:  py src/run_wtw.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import wtw_fetcher as _wtw_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_wtw.json"


def run_wtw_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    wtw_cfg = {
        "search": whole_cfg["wtw_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(wtw_cfg, _wtw_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="WTW")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[WTW] Fetched:  {total_fetched} jobs from WTW careers")
    print(f"[WTW] Matched:  {len(matched)} passed all filters")
    print(f"[WTW] New:      {len(new_matches)} not seen before")
    print(f"[WTW] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_wtw_pipeline()
        reset_failure_count("WTW")
    except Exception as exc:
        print(f"[WTW] PIPELINE ERROR: {exc}")
        print("[WTW] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("WTW", exc)
