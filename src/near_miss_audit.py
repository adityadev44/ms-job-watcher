"""Pulls recent watcher.yml GitHub Actions run logs and turns every near-miss
rejection line into structured, deduplicated, aggregated audit output.

This is a read-only analysis tool. It does not change any matching decision
-- it only reports what matcher.py / run_company.py already decided, using
the ``[co=... id=... loc=...]`` tags those modules attach to every near-miss
log line (see matcher.py::_near_miss_line). Those tags exist specifically so
this script can correctly attribute rejections under run_all.py's concurrent
per-company threads, which interleave stdout output from different companies
-- attributing by "nearest preceding [Company] Fetched: line" would silently
misattribute rows without them.

Usage:
    .venv/bin/python src/near_miss_audit.py --runs 10 --out-dir audit_output

Older log lines (from before this tagging existed) lack the [co=/id=/loc=]
suffix. Those are still parsed, but attributed to a company via a best-effort
"nearest preceding Fetched: line in this run's raw log order" heuristic and
marked confidence=low, since that heuristic is exactly the interleaving risk
described above. Prefer runs after this script's own first deploy for
anything you plan to act on.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from matcher import _contains_any, _contains_any_whole_word, _normalize_text  # noqa: E402

_ROOT = Path(__file__).parent.parent
_DEFAULT_REPO = "adityadev44/ms-job-watcher"
_DEFAULT_WORKFLOW = "watcher.yml"

# GitHub Actions `--log` format: "<job>\t<step>\t<ISO-timestamp> <message>"
_LOG_PREFIX_RE = re.compile(r"^[^\t]*\t[^\t]*\t(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s?(.*)$")

# New format (post-enrichment): "[tag]  Title (extra) [co=X id=Y loc=Z]"
_NEW_NEAR_MISS_RE = re.compile(
    r"^\[(?P<tag>[\w -]+?)\]\s+(?P<title>.*?)"
    r"(?:\s+\((?P<extra>[^()]*)\))?\s+"
    r"\[co=(?P<company>.*?) id=(?P<job_id>.*?) loc=(?P<location>.*?)\]\s*$"
)
# Old format (pre-enrichment): "[tag]  Title (extra)" -- no co=/id=/loc= suffix
_OLD_NEAR_MISS_RE = re.compile(
    r"^\[(?P<tag>[\w -]+?)\]\s+(?P<title>.*?)(?:\s+\((?P<extra>[^()]*)\))?\s*$"
)
_FETCHED_RE = re.compile(r"^\[(?P<company>.+?)\] Fetched:\s+(?P<count>\d+) jobs$")
_MATCHED_RE = re.compile(r"^\[(?P<company>.+?)\] Matched:\s+(?P<count>\d+) passed all filters$")
_DESC_UNAVAILABLE_RE = re.compile(
    r"^\[warn\] description unavailable for '(?P<title>.*)':"
)

# Titles containing one of these exclude terms are almost certainly correct
# rejections (intern/hardware/etc.) -- distinct from the "soft" terms below,
# which this session repeatedly found hiding genuine IC roles (BlackRock,
# Moody's, PepsiCo, Nutanix -- see PLAYBOOK.md).
_HARD_EXCLUDE_TERMS = {
    "intern", "internship", "trainee", "apprentice", "fresher", "graduate",
    "new grad", "university", "mechanical", "electrical", "industrial",
    "hardware", "firmware", "embedded", "datacenter technician",
    "network engineer", "sales engineer", "solutions engineer",
    "customer engineer", "support engineer", "data scientist",
}
_SOFT_EXCLUDE_TERMS = {
    "principal", "director", "vice president", "VP", "head of",
    "engineering manager", "manager",
}

# Candidate title_family additions surfaced by real findings across this
# session's onboarding waves (CitiusTech, Novartis, Lufthansa, DAZN) plus the
# ones proposed in chat. NOT applied to config.yaml -- only tested here,
# offline, against already-rejected near-miss titles, to show what each
# addition would actually flip. Uses matcher.py's own normalization so the
# result matches real matcher behavior exactly.
_CANDIDATE_TITLE_FAMILY_ADDITIONS = [
    "lead engineer",
    "lead developer",
    "lead software engineer",
    "platform engineer",
    "data engineer",
    "conversational developer",
    "cloud engineer",
    "devops engineer",
]


@dataclass
class NearMiss:
    run_id: str
    timestamp: str
    company: str
    stage: str
    title: str
    extra: str
    job_id: str
    location: str
    confidence: str  # "high" (tagged) or "low" (proximity-inferred)


@dataclass
class RunContext:
    run_id: str
    created_at: str
    company_order: list[str] = field(default_factory=list)  # order companies' Fetched: lines appeared
    last_company: str = ""


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [warn] command failed: {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        return ""
    return result.stdout


def list_recent_runs(repo: str, workflow: str, limit: int) -> list[dict]:
    out = _run([
        "gh", "run", "list", "--repo", repo, "--workflow", workflow,
        "--limit", str(limit), "--status", "completed",
        "--json", "databaseId,createdAt,conclusion,event",
    ])
    return json.loads(out) if out else []


def fetch_run_log(repo: str, run_id: int) -> str:
    return _run(["gh", "run", "view", str(run_id), "--repo", repo, "--log"])


def parse_run_log(
    run_id: str, created_at: str, raw_log: str
) -> tuple[list[NearMiss], dict[str, int]]:
    """Returns (near-miss records, per-run totals).

    Unlike near-miss lines, "[Company] Fetched: N jobs" / "Matched: M" lines
    are self-contained and unambiguous (each names its own company directly)
    -- no interleaving risk, safe to trust at face value regardless of which
    log format produced them."""
    records: list[NearMiss] = []
    totals = {"fetched": 0, "matched": 0}
    for raw_line in raw_log.splitlines():
        m = _LOG_PREFIX_RE.match(raw_line)
        if not m:
            continue
        timestamp, message = m.group(1), m.group(2)
        message = message.strip()
        if not message:
            continue

        fm = _FETCHED_RE.match(message)
        if fm:
            totals["fetched"] += int(fm.group("count"))
            continue
        mm = _MATCHED_RE.match(message)
        if mm:
            totals["matched"] += int(mm.group("count"))
            continue

        nm = _NEW_NEAR_MISS_RE.match(message)
        if nm:
            records.append(NearMiss(
                run_id=run_id, timestamp=timestamp,
                company=nm.group("company"), stage=nm.group("tag").strip(),
                title=nm.group("title").strip(), extra=(nm.group("extra") or "").strip(),
                job_id=nm.group("job_id").strip(), location=nm.group("location").strip(),
                confidence="high",
            ))
            continue

        # Old-format near-miss line (only tags this script knows about, to
        # avoid matching arbitrary bracketed log noise from other steps).
        if message[:1] == "[":
            om = _OLD_NEAR_MISS_RE.match(message)
            if om and om.group("tag").strip() in {
                "exclude", "title family", "broad-only", "react-only", "skill",
                "desc-tech",
            }:
                # Deliberately NOT using ctx_last_company here. Under
                # run_all.py's concurrent per-company threads, "most
                # recently completed company" is frequently a different
                # company than the one whose near-miss block is currently
                # printing -- a plausible-looking but wrong company name is
                # worse than an honest "don't know", since it invites
                # trusting a specific attribution that is actually a
                # coin flip. Title text and stage are unaffected by this
                # and remain fully reliable.
                records.append(NearMiss(
                    run_id=run_id, timestamp=timestamp,
                    company="UNATTRIBUTABLE (pre-tagging log)",
                    stage=om.group("tag").strip(),
                    title=om.group("title").strip(),
                    extra=(om.group("extra") or "").strip(),
                    job_id="", location="",
                    confidence="low",
                ))
    return records, totals


def dedupe(records: list[NearMiss]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in records:
        key = (r.company, r.job_id, r.title) if r.job_id else (r.company, r.title, r.stage)
        g = groups.setdefault(key, {
            "company": r.company, "stage": r.stage, "title": r.title,
            "extra": r.extra, "job_id": r.job_id, "location": r.location,
            "confidence": r.confidence, "first_seen": r.timestamp,
            "last_seen": r.timestamp, "times_seen": 0,
        })
        g["times_seen"] += 1
        g["last_seen"] = max(g["last_seen"], r.timestamp)
        g["first_seen"] = min(g["first_seen"], r.timestamp)
        if r.confidence == "high":
            g["confidence"] = "high"
            if not g["job_id"]:
                g["job_id"] = r.job_id
                g["location"] = r.location
    return list(groups.values())


def classify_exclude_reason(title: str) -> str:
    hard = _contains_any_whole_word(title, list(_HARD_EXCLUDE_TERMS))
    soft = _contains_any_whole_word(title, list(_SOFT_EXCLUDE_TERMS))
    if hard and not soft:
        return "likely correct"
    if soft:
        return "worth review"
    return "unclear"


def title_family_impact(deduped: list[dict]) -> list[dict]:
    """For every 'title family' near-miss, check which candidate addition(s)
    would make it pass, using matcher.py's own normalization/substring logic
    so this matches real matcher behavior exactly."""
    rows = []
    for row in deduped:
        if row["stage"] != "title family":
            continue
        newly_matching = [
            term for term in _CANDIDATE_TITLE_FAMILY_ADDITIONS
            if _contains_any(row["title"], [term])
        ]
        if newly_matching:
            rows.append({**row, "would_pass_with": ", ".join(newly_matching)})
    # Rank by how many times seen (proxy for how many real postings this affects)
    rows.sort(key=lambda r: r["times_seen"], reverse=True)
    return rows


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=_DEFAULT_REPO)
    parser.add_argument("--workflow", default=_DEFAULT_WORKFLOW)
    parser.add_argument("--runs", type=int, default=10, help="how many recent completed runs to pull")
    parser.add_argument("--out-dir", type=Path, default=_ROOT / "audit_output")
    args = parser.parse_args(argv)

    print(f"Listing last {args.runs} completed runs of {args.workflow} on {args.repo}...")
    runs = list_recent_runs(args.repo, args.workflow, args.runs)
    if not runs:
        print("No completed runs found.", file=sys.stderr)
        return 1
    print(f"Found {len(runs)} runs spanning {runs[-1]['createdAt']} to {runs[0]['createdAt']}")

    all_records: list[NearMiss] = []
    run_totals: list[dict[str, int]] = []
    for i, run in enumerate(runs, 1):
        run_id = str(run["databaseId"])
        print(f"  [{i}/{len(runs)}] fetching log for run {run_id}...")
        raw_log = fetch_run_log(args.repo, run["databaseId"])
        if not raw_log:
            continue
        records, totals = parse_run_log(run_id, run["createdAt"], raw_log)
        print(f"      {len(records)} near-miss lines, fetched={totals['fetched']} matched={totals['matched']}")
        all_records.extend(records)
        run_totals.append(totals)

    print(f"\nTotal near-miss lines across all runs: {len(all_records)}")
    deduped = dedupe(all_records)
    print(f"Deduplicated to {len(deduped)} unique (company, job) rejections")

    high_conf = sum(1 for r in deduped if r["confidence"] == "high")
    low_conf = len(deduped) - high_conf
    print(f"  {high_conf} high-confidence (tagged), {low_conf} low-confidence (proximity-inferred, pre-enrichment logs)")

    # --- Aggregations ---
    by_stage = Counter(r["stage"] for r in deduped)
    by_company = Counter(r["company"] for r in deduped)

    for row in deduped:
        row["exclude_review"] = (
            classify_exclude_reason(row["title"]) if row["stage"] == "exclude" else ""
        )

    impact_rows = title_family_impact(deduped)

    # --- Write output files ---
    out = args.out_dir
    write_csv(
        out / "near_misses_deduped.csv", deduped,
        ["company", "stage", "title", "extra", "job_id", "location",
         "confidence", "exclude_review", "times_seen", "first_seen", "last_seen"],
    )
    write_csv(
        out / "aggregate_by_stage.csv",
        [{"stage": k, "count": v} for k, v in by_stage.most_common()],
        ["stage", "count"],
    )
    write_csv(
        out / "aggregate_by_company.csv",
        [{"company": k, "count": v} for k, v in by_company.most_common()],
        ["company", "count"],
    )
    write_csv(
        out / "title_family_candidate_impact.csv", impact_rows,
        ["title", "would_pass_with", "company", "job_id", "location",
         "times_seen", "confidence", "first_seen", "last_seen"],
    )

    worth_review = [r for r in deduped if r.get("exclude_review") == "worth review"]
    write_csv(
        out / "exclude_worth_review.csv", worth_review,
        ["company", "title", "extra", "job_id", "location", "times_seen",
         "confidence", "first_seen", "last_seen"],
    )

    avg_fetched = sum(t["fetched"] for t in run_totals) / len(run_totals) if run_totals else 0
    avg_matched = sum(t["matched"] for t in run_totals) / len(run_totals) if run_totals else 0
    match_rate = (avg_matched / avg_fetched * 100) if avg_fetched else 0

    summary_lines = [
        "# Near-Miss Audit Summary",
        "",
        f"Runs analyzed: {len(runs)} (from {runs[-1]['createdAt']} to {runs[0]['createdAt']})",
        f"Average per run: {avg_fetched:.0f} jobs fetched, {avg_matched:.1f} passed all filters "
        f"({match_rate:.2f}% match rate). This counts every still-open posting again each "
        "cycle, not distinct jobs — a rough per-cycle yield, not a cumulative total.",
        f"Total near-miss lines: {len(all_records)}",
        f"Deduplicated unique rejections: {len(deduped)} "
        f"({high_conf} high-confidence tagged, {low_conf} low-confidence proximity-inferred)",
        "",
        "## By rejection stage",
        "",
    ]
    for stage, count in by_stage.most_common():
        summary_lines.append(f"- `{stage}`: {count}")
    summary_lines += ["", "## Top companies by near-miss volume", ""]
    if high_conf == 0:
        summary_lines.append(
            "_All analyzed runs predate the `[co=/id=/loc=]` tagging deploy, so "
            "per-company attribution isn't available yet — everything below is one "
            "honest `UNATTRIBUTABLE` bucket rather than a guess. Re-run this audit "
            "after a few scheduled runs post-deploy for a real per-company breakdown._",
            "",
        )
    for company, count in by_company.most_common(15):
        summary_lines.append(f"- {company}: {count}")
    summary_lines += [
        "",
        "## title_family candidate impact",
        "",
        f"{len(impact_rows)} distinct rejected titles would flip to passing if any of the "
        f"candidate additions below were added to `title_family` in config.yaml. See "
        "`title_family_candidate_impact.csv` for the full ranked list — this is analysis "
        "only, nothing has been changed in config.yaml.",
        "",
        "Candidates tested: " + ", ".join(f"`{t}`" for t in _CANDIDATE_TITLE_FAMILY_ADDITIONS),
        "",
        "## `exclude` rejections worth a second look",
        "",
        f"{len(worth_review)} rejections were excluded by a 'soft' term (manager/director/VP/"
        "principal/head of) that this repo has repeatedly found hiding genuine IC-level AI/.NET "
        "roles (BlackRock, Moody's, PepsiCo, Nutanix — see PLAYBOOK.md). See "
        "`exclude_worth_review.csv`. This is NOT a claim these are all false negatives — just "
        "a pre-filtered shortlist worth a human read before any exclude_terms change.",
        "",
        "## What this audit does NOT cover",
        "",
        "- **Roles at companies not yet onboarded.** This audit only sees roles from the 153 "
        "companies already in the registry — it has no visibility into roles at any company "
        "without a fetcher. That is a separate workstream (company expansion), not something "
        "this log-based audit can measure.",
        "- **Roles correctly rejected.** Every row here is something the matcher rejected; most "
        "are correct rejections (real interns, real hardware roles, real non-tech titles). "
        "The `exclude_review` column offers a rough first pass at separating likely-correct "
        "rejections from ones worth a second look, but it's a heuristic, not a verdict.",
        "- **Historical depth.** Low-confidence rows come from logs predating the "
        "`[co=/id=/loc=]` tagging added alongside this script. A true 30-day trend needs "
        "runs to accumulate after that point — re-run this script periodically and diff "
        "the output against a saved copy to build that history.",
        "",
        "No matcher/config changes were made by this script. All figures above are read-only "
        "analysis for human review.",
    ]
    (out / "summary.md").write_text("\n".join(summary_lines), encoding="utf-8")

    print(f"\nWrote audit output to {out}/")
    print("  near_misses_deduped.csv, aggregate_by_stage.csv, aggregate_by_company.csv,")
    print("  title_family_candidate_impact.csv, exclude_worth_review.csv, summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
