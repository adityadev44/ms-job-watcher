"""
Amazon job-watcher pipeline entry point.

Runs independently of the Microsoft and Optum pipelines:
  - Uses amazon_fetcher instead of fetcher / optum_fetcher
  - Writes to seen_jobs_amazon.json instead of seen_jobs.json

Run:  py src/run_amazon.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import amazon_fetcher as _amazon_mod
from matcher import find_matching_jobs, load_config
from notifier import notify
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_amazon.json"


def run_amazon_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    amazon_cfg = {
        "search": whole_cfg["amazon_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(amazon_cfg, _amazon_mod)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Amazon")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Amazon] Fetched:  {total_fetched} jobs from Amazon careers")
    print(f"[Amazon] Matched:  {len(matched)} passed all filters")
    print(f"[Amazon] New:      {len(new_matches)} not seen before")
    print(f"[Amazon] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    run_amazon_pipeline()
