"""Git merge driver for seen_jobs*.json files.

These files are append-only dedup ledgers: a flat JSON array of job IDs
already alerted on. They only ever grow, so on a merge conflict the
correct resolution is always the union of both sides' entries — never a
manual pick. This lets concurrent CI runs (or a concurrent local push)
resolve automatically instead of failing the "Save state" step.

Registered via .gitattributes (`seen_jobs*.json merge=jsonunion`) and
`git config merge.jsonunion.driver`, git invokes this as:
    merge_seen_jobs_union.py %O %A %B
where %O = common ancestor, %A = current (ours), %B = other (theirs).
Git treats a zero exit as resolved and takes the content git wrote to %A.
"""
import json
import sys


def _load(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def main():
    ours_path, theirs_path = sys.argv[2], sys.argv[3]
    ours = _load(ours_path)
    theirs = _load(theirs_path)

    if not isinstance(ours, list) or not isinstance(theirs, list):
        return 1  # not the shape we expect — let git fall back to a normal conflict

    merged = sorted(set(ours) | set(theirs), key=str)

    with open(ours_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
