"""
Full pipeline entry point.

Flow: fetch jobs → match against config → skip already-seen IDs → alert on new matches
      → record new IDs in seen_jobs.json.

Run:  py src/main.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

from matcher import find_matching_jobs
from notifier import DeliveryResult, notify, notify_pipeline_error, reset_failure_count

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs.json"


def load_seen_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_ids(path: Path, ids: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        json.dump(sorted(ids), f, indent=2)
        f.write("\n")
        temporary_path = f.name
    os.replace(temporary_path, path)


def run_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    seen_ids = load_seen_ids(seen_path)

    total_fetched, matched = find_matching_jobs(config_path, known_ids=seen_ids)
    # Keep this guard for custom/legacy matcher implementations that do not
    # yet support early pruning.
    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        delivery = notify(new_matches)
        # ``None`` preserves compatibility with legacy notifiers and simple
        # test doubles. DeliveryResult exposes the explicit modern contract.
        if isinstance(delivery, DeliveryResult):
            should_mark_seen = delivery.should_mark_seen
            alert_sent = delivery.any_succeeded
        elif isinstance(delivery, bool):
            should_mark_seen = alert_sent = delivery
        else:
            should_mark_seen = alert_sent = True
        if should_mark_seen:
            seen_ids.update(j["id"] for j in new_matches)
            save_seen_ids(seen_path, seen_ids)

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


def cli_main() -> int:
    """Run once and return a process status suitable for a batch launcher."""
    try:
        run_pipeline()
        reset_failure_count("Microsoft")
        return 0
    except Exception as exc:
        print(f"[MS] PIPELINE ERROR: {exc}")
        notify_pipeline_error("Microsoft", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
