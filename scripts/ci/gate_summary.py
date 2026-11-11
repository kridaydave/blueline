#!/usr/bin/env python3
"""Write a small gate summary JSON fragment and append to GITHUB_STEP_SUMMARY.

Stdlib only. Tolerant: missing env/files should not crash the gate.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_summary(md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(md)
            if not md.endswith("\n"):
                fh.write("\n")
    except OSError:
        pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--category", required=True)
    p.add_argument("--priority", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--result", required=True, help="success|failure|cancelled|skipped|unknown")
    p.add_argument("--reason", default="")
    p.add_argument("--command", default="")
    p.add_argument("--evidence", default="")
    p.add_argument("--fix-first", default="")
    p.add_argument("--ci-job", default="")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    data = {
        "schema_version": 1,
        "kind": "gate",
        "category": args.category,
        "priority": args.priority,
        "job": args.job,
        "ci_job": args.ci_job or args.job,
        "result": args.result,
        "reason": args.reason,
        "command": args.command,
        "evidence": args.evidence,
        "fix_first": args.fix_first,
        "sha": os.environ.get("GITHUB_SHA", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "created_at": utc_now(),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    icon = {"success": "PASS", "failure": "FAIL", "cancelled": "CANCELLED", "skipped": "SKIP"}.get(
        args.result, args.result.upper()
    )
    md = [f"## {args.priority} {args.category} — {args.job}", "", f"**Result:** {icon}"]
    if args.reason:
        md.append(f"**Reason:** {args.reason}")
    if args.fix_first:
        md.append(f"**Fix first:** {args.fix_first}")
    if args.command:
        md.append(f"**Command:** `{args.command}`")
    if args.evidence:
        md.append(f"**Evidence:** {args.evidence}")
    md.append("")
    append_summary("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
