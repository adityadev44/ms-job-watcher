"""
Accenture job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses accenture_fetcher (Workday REST API)
  - Writes to seen_jobs_accenture.json

Run:  py src/run_accenture.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import accenture_fetcher as _accenture_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error, reset_failure_count
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_accenture.json"


def run_accenture_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    accenture_cfg = {
        "search": whole_cfg["accenture_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(accenture_cfg, _accenture_mod)

    # Accenture-specific: require the tech stack to appear in the job title.
    # Accenture posts thousands of India roles; generic titles like
    # "Custom Software Engineer" are rarely .NET. This filter keeps precision high.
    tech_terms = whole_cfg["accenture_search"].get("require_tech_in_title", [])
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
            print("Accenture title-tech filtered out (near-misses):")
            for line in title_dropped:
                print(f"  {line}")
        matched = title_passed

    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Accenture")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Accenture] Fetched:  {total_fetched} jobs from Accenture careers")
    print(f"[Accenture] Matched:  {len(matched)} passed all filters")
    print(f"[Accenture] New:      {len(new_matches)} not seen before")
    print(f"[Accenture] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_accenture_pipeline()
        reset_failure_count("Accenture")
    except Exception as exc:
        print(f"[Accenture] PIPELINE ERROR: {exc}")
        print("[Accenture] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Accenture", exc)
