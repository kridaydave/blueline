#!/usr/bin/env python3
"""Summarize a cargo-mutants shard run for CI inspectability.

Best-effort: cargo-mutants output formats vary by version; missing files are OK.
Never fails the build by itself unless --strict is passed.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def count_outcomes(obj: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if obj is None:
        return counts
    # cargo-mutants outcomes.json may be {"outcomes":[{"outcome":"Caught"|"Missed"|...}]}
    # or similar structures. Be tolerant.
    items = None
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        for key in ("outcomes", "mutants", "results"):
            if isinstance(obj.get(key), list):
                items = obj[key]
                break
        if items is None and "summary" in obj and isinstance(obj["summary"], dict):
            for k, v in obj["summary"].items():
                if isinstance(v, int):
                    counts[k.lower()] = counts.get(k.lower(), 0) + v
            return counts
    if not items:
        return counts
    for item in items:
        if not isinstance(item, dict):
            continue
        outcome = item.get("outcome") or item.get("summary") or item.get("result")
        if isinstance(outcome, str):
            k = outcome.lower()
            counts[k] = counts.get(k, 0) + 1
    return counts


def classify(job_result: str, passes: list[dict[str, Any]], counts: dict[str, int]) -> str:
    missed = counts.get("missed", 0) + counts.get("survived", 0)
    timeout = counts.get("timeout", 0) + counts.get("time_out", 0)
    if missed > 0:
        return "survivor"
    if timeout > 0:
        return "timeout"
    if job_result == "cancelled":
        return "infra_cancelled"
    if job_result == "failure":
        # could be unviable-only, tool error, or unknown
        if counts.get("unviable", 0) > 0 and missed == 0 and timeout == 0:
            return "unviable_or_tool_failure"
        return "tool_error_or_failure"
    if job_result == "success":
        if sum(counts.values()) == 0:
            return "no_mutants_or_unknown"
        return "clean"
    return "unknown"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True)
    p.add_argument("--shard", required=True)
    p.add_argument("--shard-total", default="32")
    p.add_argument("--job-result", default="unknown")
    p.add_argument("--diff-base", default="")
    p.add_argument("--mutants-out", action="append", default=[])
    p.add_argument("--outcomes-json", action="append", default=[])
    p.add_argument("--pass", action="append", default=[], help="name=exit_code form")
    p.add_argument("--out", required=True)
    p.add_argument("--artifact-name", default="")
    p.add_argument("--strict", action="store_true")
    args = p.parse_args()

    passes = []
    for spec in args.passes if False else args.__dict__["pass"]:
        if "=" in spec:
            name, code = spec.rsplit("=", 1)
            try:
                exit_code = int(code)
            except ValueError:
                exit_code = None
        else:
            name, exit_code = spec, None
        passes.append({"name": name, "exit_code": exit_code})

    merged_counts: dict[str, int] = {}
    outcomes_sources = []
    for rel in args.outcomes_json:
        path = Path(rel)
        if not path.exists():
            continue
        outcomes_sources.append(str(path))
        obj = load_json(path)
        for k, v in count_outcomes(obj).items():
            merged_counts[k] = merged_counts.get(k, 0) + v

    mutants_out = [str(p) for p in args.mutants_out if Path(p).exists()]
    root = classify(args.job_result, passes, merged_counts)
    data = {
        "schema_version": 1,
        "kind": "mutants_shard",
        "category": "gate.security.mutation",
        "priority": "P1",
        "event": args.event,
        "shard": args.shard,
        "shard_total": args.shard_total,
        "job_result": args.job_result,
        "root_cause_class": root,
        "diff_base": args.diff_base,
        "passes": passes,
        "outcome_counts": merged_counts,
        "outcomes_sources": outcomes_sources,
        "mutants_out_files": mutants_out,
        "artifact_name": args.artifact_name,
        "sha": os.environ.get("GITHUB_SHA", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "created_at": utc_now(),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rows = [
        f"## P1 gate.security.mutation — shard {args.shard}/{args.shard_total} ({args.event})",
        "",
        f"**Job result:** {args.job_result}",
        f"**Root cause class:** `{root}`",
        f"**Diff base:** `{args.diff_base}`" if args.diff_base else None,
    ]
    rows = [r for r in rows if r is not None]
    if passes:
        rows.append("**Passes:** " + ", ".join(f"`{p['name']}`={p.get('exit_code')}" for p in passes))
    if merged_counts:
        rows.append("**Outcome counts:** " + ", ".join(f"{k}={v}" for k, v in sorted(merged_counts.items())))
    else:
        rows.append("**Outcome counts:** unavailable (no parseable outcomes.json); inspect `mutants.out` artifact.")
    if args.job_result == "cancelled" or root in ("infra_cancelled", "timeout"):
        rows.append("")
        rows.append(
            "Cancelled/timeouts are **unknown fate**, not survivors. Check job timeout (100m), per-mutant timeout (300s), and shard logs."
        )
    rows.append("")
    append_summary("\n".join(rows))

    return 0 if not args.strict else (0 if args.job_result == "success" and root in ("clean", "no_mutants_or_unknown") else 1)


if __name__ == "__main__":
    raise SystemExit(main())
