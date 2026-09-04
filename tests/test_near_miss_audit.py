"""
Tests for src/near_miss_audit.py.

All tests use fabricated log text -- no live `gh` calls, no network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import near_miss_audit
from near_miss_audit import (
    classify_exclude_reason,
    dedupe,
    main,
    parse_run_log,
    title_family_impact,
)


def _gh_line(job: str, step: str, ts: str, message: str) -> str:
    return f"{job}\t{step}\t{ts} {message}"


def test_parse_run_log_extracts_new_format_tagged_lines():
    log = "\n".join([
        _gh_line("watch", "Run all watchers", "2026-09-04T00:00:00.000000Z",
                  "[exclude]       Principal Engineer [co=Acme id=42 loc=Bengaluru, India]"),
        _gh_line("watch", "Run all watchers", "2026-09-04T00:00:01.000000Z",
                  "[title family]  Data Platform Engineer [co=Acme id=43 loc=Mumbai, India]"),
        _gh_line("watch", "Run all watchers", "2026-09-04T00:00:02.000000Z",
                  "[Acme] Fetched:  2 jobs"),
        _gh_line("watch", "Run all watchers", "2026-09-04T00:00:03.000000Z",
                  "[Acme] Matched:  0 passed all filters"),
    ])
    records, totals = parse_run_log("123", "2026-09-04T00:00:00Z", log)

    assert len(records) == 2
    assert records[0].company == "Acme"
    assert records[0].stage == "exclude"
    assert records[0].title == "Principal Engineer"
    assert records[0].job_id == "42"
    assert records[0].location == "Bengaluru, India"
    assert records[0].confidence == "high"
    assert totals == {"fetched": 2, "matched": 0}


def test_parse_run_log_falls_back_to_old_format_as_unattributable():
    log = _gh_line(
        "watch", "Run all watchers", "2026-09-04T00:00:00.000000Z",
        "[skill]         Software Engineer (desc=100 chars, skills_found=none)",
    )
    records, _ = parse_run_log("123", "2026-09-04T00:00:00Z", log)

    assert len(records) == 1
    assert records[0].confidence == "low"
    assert records[0].company == "UNATTRIBUTABLE (pre-tagging log)"
    assert records[0].title == "Software Engineer"
    assert records[0].job_id == ""


def test_parse_run_log_ignores_unrelated_bracketed_lines():
    # A bracketed line that isn't a known near-miss tag (e.g. a GitHub
    # Actions group marker) must not be misparsed as a rejection.
    log = _gh_line(
        "watch", "Run all watchers", "2026-09-04T00:00:00.000000Z",
        "[group] Setting up job",
    )
    records, _ = parse_run_log("123", "2026-09-04T00:00:00Z", log)
    assert records == []


def test_dedupe_counts_repeats_and_keeps_first_high_confidence():
    log = "\n".join([
        _gh_line("watch", "s", "2026-09-04T00:00:00.000000Z",
                  "[title family]  Data Engineer [co=Acme id=1 loc=Pune, India]"),
        _gh_line("watch", "s", "2026-09-04T01:00:00.000000Z",
                  "[title family]  Data Engineer [co=Acme id=1 loc=Pune, India]"),
    ])
    records, _ = parse_run_log("123", "2026-09-04T00:00:00Z", log)
    deduped = dedupe(records)

    assert len(deduped) == 1
    assert deduped[0]["times_seen"] == 2
    assert deduped[0]["first_seen"] == "2026-09-04T00:00:00.000000Z"
    assert deduped[0]["last_seen"] == "2026-09-04T01:00:00.000000Z"


def test_title_family_impact_uses_real_matcher_normalization():
    log = "\n".join([
        _gh_line("watch", "s", "2026-09-04T00:00:00.000000Z",
                  "[title family]  Lead Engineer [co=Acme id=1 loc=Pune, India]"),
        _gh_line("watch", "s", "2026-09-04T00:00:01.000000Z",
                  "[title family]  Data Platform Engineer [co=Acme id=2 loc=Pune, India]"),
        _gh_line("watch", "s", "2026-09-04T00:00:02.000000Z",
                  "[title family]  Product Manager [co=Acme id=3 loc=Pune, India]"),
    ])
    records, _ = parse_run_log("123", "2026-09-04T00:00:00Z", log)
    deduped = dedupe(records)
    impact = title_family_impact(deduped)

    titles = {row["title"]: row["would_pass_with"] for row in impact}
    assert "lead engineer" in titles["Lead Engineer"]
    assert "platform engineer" in titles["Data Platform Engineer"]
    # "Product Manager" matches no candidate addition -- must not appear.
    assert "Product Manager" not in titles


def test_classify_exclude_reason_distinguishes_hard_and_soft_terms():
    assert classify_exclude_reason("Software Engineering Intern") == "likely correct"
    assert classify_exclude_reason("Engineering Manager, Platform") == "worth review"
    assert classify_exclude_reason("Some Unrelated Title") == "unclear"


def _fake_run(databaseId, createdAt):
    return {"databaseId": databaseId, "createdAt": createdAt, "conclusion": "success", "event": "schedule"}


def test_main_writes_all_output_files_end_to_end(tmp_path, monkeypatch):
    """Full main() path, all-old-format input (high_conf == 0) -- this is
    exactly the combination that shipped a live TypeError in production
    (summary_lines.append() called with two positional args instead of
    one) because it was only exercised manually, not under test. Covering
    it here so a future edit to that code path fails CI, not a real run."""
    runs = [_fake_run(1, "2026-09-04T00:00:00Z")]
    log = _gh_line(
        "watch", "s", "2026-09-04T00:00:00.000000Z",
        "[title family]  Data Engineer (skills=Python)",
    )
    monkeypatch.setattr(near_miss_audit, "list_recent_runs", lambda repo, wf, n: runs)
    monkeypatch.setattr(near_miss_audit, "fetch_run_log", lambda repo, run_id: log)

    out_dir = tmp_path / "audit_output"
    rc = main(["--runs", "1", "--out-dir", str(out_dir)])

    assert rc == 0
    for name in (
        "near_misses_deduped.csv", "aggregate_by_stage.csv",
        "aggregate_by_company.csv", "title_family_candidate_impact.csv",
        "exclude_worth_review.csv", "summary.md",
    ):
        assert (out_dir / name).exists(), f"missing {name}"
    summary = (out_dir / "summary.md").read_text()
    assert "UNATTRIBUTABLE" in summary or "tagging deploy" in summary


def test_main_writes_all_output_files_with_high_confidence_data(tmp_path, monkeypatch):
    """Same as above but with tagged (post-enrichment) input, so
    high_conf > 0 and the other branch of the same summary block runs."""
    runs = [_fake_run(1, "2026-09-04T00:00:00Z")]
    log = _gh_line(
        "watch", "s", "2026-09-04T00:00:00.000000Z",
        "[title family]  Data Engineer [co=Acme id=1 loc=Pune, India]",
    )
    monkeypatch.setattr(near_miss_audit, "list_recent_runs", lambda repo, wf, n: runs)
    monkeypatch.setattr(near_miss_audit, "fetch_run_log", lambda repo, run_id: log)

    out_dir = tmp_path / "audit_output"
    rc = main(["--runs", "1", "--out-dir", str(out_dir)])

    assert rc == 0
    assert "Acme" in (out_dir / "summary.md").read_text()
