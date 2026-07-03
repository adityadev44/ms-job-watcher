"""
LTIMindtree job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses ltimindtree_fetcher instead of other fetchers
  - Writes to seen_jobs_ltimindtree.json instead of seen_jobs.json

Run:  py src/run_ltimindtree.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import ltimindtree_fetcher as _ltimindtree_mod
from matcher import find_matching_jobs, load_config, _normalize_text
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_ltimindtree.json"


def run_ltimindtree_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    ltimindtree_cfg = {
        "search": whole_cfg["ltimindtree_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(ltimindtree_cfg, _ltimindtree_mod)

    # LTIMindtree-only: require a core .NET/C#/ASP.NET term in the
    # description, not just any shared primary_skill — generic level-banded
    # titles ("Specialist - Software Engineering") give no tech signal on
    # their own, and this is a large, multi-stack IT-services shop.
    tech_terms = whole_cfg["ltimindtree_search"].get("require_tech_in_description", [])
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
            print("LTIMindtree description-tech filtered out (near-misses):")
            for line in desc_dropped:
                print(f"  {line}")
        matched = desc_passed

    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="LTIMindtree")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[LTIMindtree] Fetched:  {total_fetched} jobs from LTIMindtree careers")
    print(f"[LTIMindtree] Matched:  {len(matched)} passed all filters")
    print(f"[LTIMindtree] New:      {len(new_matches)} not seen before")
    print(f"[LTIMindtree] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_ltimindtree_pipeline()
        reset_failure_count("LTIMindtree")
    except Exception as exc:
        print(f"[LTIMindtree] PIPELINE ERROR: {exc}")
        print("[LTIMindtree] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("LTIMindtree", exc)
