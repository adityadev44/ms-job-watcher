"""
State Street job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses statestreet_fetcher instead of other fetchers
  - Writes to seen_jobs_statestreet.json instead of seen_jobs.json

Run:  py src/run_statestreet.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import statestreet_fetcher as _statestreet_mod
from matcher import find_matching_jobs, load_config, _normalize_text
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_statestreet.json"


def run_statestreet_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    statestreet_cfg = {
        "search": whole_cfg["statestreet_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(statestreet_cfg, _statestreet_mod)

    # State Street-only: require a core .NET/C#/ASP.NET term in the
    # description, not just any shared primary_skill (SQL Server/EF alone
    # could easily be a Java role).
    tech_terms = whole_cfg["statestreet_search"].get("require_tech_in_description", [])
    if tech_terms:
        normed_terms = [_normalize_text(t) for t in tech_terms]
        desc_passed = []
        desc_dropped = []
        for j in matched:
            normed_desc = _normalize_text(j.get("description", ""))
            if any(t in normed_desc for t in normed_terms):
                desc_passed.append(j)
            else:
                desc_dropped.append(f"[desc-tech]     {j['title']}")
        if desc_dropped:
            print("State Street description-tech filtered out (near-misses):")
            for line in desc_dropped:
                print(f"  {line}")
        matched = desc_passed

    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="State Street")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[State Street] Fetched:  {total_fetched} jobs from State Street careers")
    print(f"[State Street] Matched:  {len(matched)} passed all filters")
    print(f"[State Street] New:      {len(new_matches)} not seen before")
    print(f"[State Street] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_statestreet_pipeline()
        reset_failure_count("State Street")
    except Exception as exc:
        print(f"[State Street] PIPELINE ERROR: {exc}")
        print(f"[State Street] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("State Street", exc)
