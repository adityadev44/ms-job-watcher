"""
Capgemini job-watcher pipeline entry point.

Runs independently of all other pipelines:
  - Uses capgemini_fetcher (SAP SuccessFactors J2W HTML scraping)
  - Writes to seen_jobs_capgemini.json
  - Layer 4 filter active: require_tech_in_title (.NET / C# terms must appear
    in the job title). Capgemini posts thousands of India jobs; without this
    filter almost none would be .NET roles.

Run:  py src/run_capgemini.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import capgemini_fetcher as _cap_mod
from matcher import find_matching_jobs, load_config
from notifier import notify, notify_pipeline_error
from main import load_seen_ids, save_seen_ids

_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT / "config.yaml"
_DEFAULT_SEEN = _ROOT / "seen_jobs_capgemini.json"


def run_capgemini_pipeline(
    config_path: Path = _DEFAULT_CONFIG,
    seen_path: Path = _DEFAULT_SEEN,
) -> dict:
    whole_cfg = load_config(config_path)
    seen_ids = load_seen_ids(seen_path)

    cap_cfg = {
        "search": whole_cfg["capgemini_search"],
        "matching": whole_cfg.get("matching", {}),
    }

    total_fetched, matched = find_matching_jobs(cap_cfg, _cap_mod)

    # Capgemini-specific: require the tech stack to appear in the job title.
    # Capgemini posts thousands of India jobs; generic titles like
    # "Lead Software Engineer" are mostly SAP/Java/Python roles that mention
    # a .NET skill in passing. This 4th filter keeps precision high.
    tech_terms = whole_cfg["capgemini_search"].get("require_tech_in_title", [])
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
            print("Capgemini title-tech filtered out (near-misses):")
            for line in title_dropped:
                print(f"  {line}")
        matched = title_passed

    new_matches = [j for j in matched if j["id"] not in seen_ids]

    alert_sent = False
    if new_matches:
        notify(new_matches, source="Capgemini")
        seen_ids.update(j["id"] for j in new_matches)
        save_seen_ids(seen_path, seen_ids)
        alert_sent = True

    print(f"[Capgemini] Fetched:  {total_fetched} jobs from Capgemini careers")
    print(f"[Capgemini] Matched:  {len(matched)} passed all filters")
    print(f"[Capgemini] New:      {len(new_matches)} not seen before")
    print(f"[Capgemini] Alert:    {'sent' if alert_sent else 'not sent (no new matches)'}")

    return {
        "total_fetched": total_fetched,
        "matched": len(matched),
        "new": len(new_matches),
        "alert_sent": alert_sent,
    }


if __name__ == "__main__":
    try:
        run_capgemini_pipeline()
    except Exception as exc:
        print(f"[Capgemini] PIPELINE ERROR: {exc}")
        print("[Capgemini] Exiting cleanly to avoid blocking other pipelines.")
        notify_pipeline_error("Capgemini", exc)
