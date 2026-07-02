"""
TCS job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses tcs_fetcher (iBegin proprietary REST API)
  - Writes to seen_jobs_tcs.json

Run:  py src/run_tcs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import tcs_fetcher as _tcs_mod
from matcher import find_matching_jobs, load_config, _normalize_text
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_tcs.json"


def run_tcs_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    tcs_cfg = {
        "search": whole_cfg["tcs_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(tcs_cfg, _tcs_mod)

    # TCS-specific: require a core .NET/C#/ASP.NET term in the description,
    # not just any shared primary_skill. TCS posts thousands of India jobs;
    # generic titles like "Senior Software Engineer" are almost never
    # .NET/C# roles. This 4th filter keeps alerts precise.
    tech_terms = whole_cfg["tcs_search"].get("require_tech_in_description", [])
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
            print("TCS description-tech filtered out (near-misses):")
            for line in desc_dropped:
                print(f"  {line}")
        matched = desc_passed

    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="TCS")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[TCS] Fetched:  {total_fetched} jobs from TCS careers")
    print(f"[TCS] Matched:  {len(matched)} passed all filters")
    print(f"[TCS] New:      {len(new_matches)} not seen before")
    print(f"[TCS] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_tcs_pipeline()
        reset_failure_count("TCS")
    except Exception as exc:
        print(f"[TCS] PIPELINE ERROR: {exc}")
        print("[TCS] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("TCS", exc)
