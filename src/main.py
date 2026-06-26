"""
Full pipeline entry point.

Flow: fetch jobs → match against config → skip already-seen IDs → alert on new matches
      → record new IDs in seen_jobs.json.

Run:  py src/main.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

from matcher import find_matching_jobs
from notifier import notify, notify_pipeline_error

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs.json"


def load_seen_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_ids(path: Path, ids: set[str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def run_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    seen_ids = load_seen_ids(seen_path)

    total_fetched, matched = find_matching_jobs(config_path)
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches)
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"Fetched:  {total_fetched} jobs from Microsoft careers")
    print(f"Matched:  {len(matched)} passed all filters")
    print(f"New:      {len(new_matches)} not seen before")
    print(f"Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_pipeline()
    except Exception as exc:
        print(f"[MS] PIPELINE ERROR: {exc}")
        print("[MS] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Microsoft", exc)
