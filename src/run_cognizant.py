"""
Cognizant job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses cognizant_fetcher (Umbraco RSS feed, all India jobs cached once)
  - Writes to seen_jobs_cognizant.json

Run:  py src/run_cognizant.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import cognizant_fetcher as _cog_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_cognizant.json"


def run_cognizant_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    cog_cfg = {
        "search": whole_cfg["cognizant_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(cog_cfg, _cog_mod)

    # Cognizant-only: require the tech stack to appear in the job title.
    # Cognizant posts thousands of India jobs with generic titles like
    # "Senior Developer" that rarely mention the actual tech stack.
    # Requiring explicit .NET/C# in the title keeps precision high.
    tech_terms = whole_cfg["cognizant_search"].get("require_tech_in_title", [])
    if tech_terms:
        title_passed = []
        title_dropped = []
        for j in matched:
            t = j["title"].lower()
            if any(term.lower() in t for term in tech_terms):
                title_passed.append(j)
            else:
                title_dropped.append(f"[title-tech]    {j['title']}")
        if title_dropped:
            print("Cognizant title-tech filtered out (near-misses):")
            for line in title_dropped:
                print(f"  {line}")
        matched = title_passed

    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Cognizant")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Cognizant] Fetched:  {total_fetched} jobs from Cognizant careers")
    print(f"[Cognizant] Matched:  {len(matched)} passed all filters")
    print(f"[Cognizant] New:      {len(new_matches)} not seen before")
    print(f"[Cognizant] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_cognizant_pipeline()
        reset_failure_count("Cognizant")
    except Exception as exc:
        print(f"[Cognizant] PIPELINE ERROR: {exc}")
        print("[Cognizant] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Cognizant", exc)
