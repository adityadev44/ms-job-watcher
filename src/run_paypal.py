"""
PayPal job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses paypal_fetcher instead of other fetchers
  - Writes to seen_jobs_paypal.json instead of seen_jobs.json

Run:  py src/run_paypal.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import paypal_fetcher as _paypal_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_paypal.json"


def run_paypal_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    paypal_cfg = {
        "search": whole_cfg["paypal_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(paypal_cfg, _paypal_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="PayPal")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[PayPal] Fetched:  {total_fetched} jobs from PayPal careers")
    print(f"[PayPal] Matched:  {len(matched)} passed all filters")
    print(f"[PayPal] New:      {len(new_matches)} not seen before")
    print(f"[PayPal] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_paypal_pipeline()
        reset_failure_count("PayPal")
    except Exception as exc:
        print(f"[PayPal] PIPELINE ERROR: {exc}")
        print(f"[PayPal] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("PayPal", exc)
