#!/usr/bin/env python3
"""Summarize a cargo-mutants shard run for CI inspectability.

Best-effort: cargo-mutants output formats vary by version; missing files are OK.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTCOMES = {"success", "failure", "cancelled", "skipped", "unknown"}


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
    any_pass_failed = any(p.get("outcome") == "failure" for p in passes)
    any_pass_cancelled = any(p.get("outcome") == "cancelled" for p in passes)
    if missed > 0:
        return "survivor"
    if timeout > 0:
        return "timeout"
    if any_pass_cancelled or job_result == "cancelled":
        return "infra_cancelled"
    if any_pass_failed or job_result == "failure":
        if sum(counts.values()) == 0:
            return "survivor_or_unknown"
        if counts.get("unviable", 0) > 0 and missed == 0 and timeout == 0:
            return "unviable_or_tool_failure"
        return "tool_error_or_failure"
    if job_result == "success":
        return "no_mutants_or_unknown" if sum(counts.values()) == 0 else "clean"
    return "unknown"


def normalized_result(root: str, job_result: str, passes: list[dict[str, Any]]) -> str:
    if root in {"survivor", "timeout", "infra_cancelled", "survivor_or_unknown", "tool_error_or_failure", "unviable_or_tool_failure"}:
        return "failure"
    if any(p.get("outcome") in {"failure", "cancelled"} for p in passes):
        return "failure"
    if job_result in OUTCOMES:
        return job_result
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
    p.add_argument("--pass", dest="passes", action="append", default=[], help="name=outcome (success|failure|cancelled|skipped)")
    p.add_argument("--out", required=True)
    p.add_argument("--artifact-name", default="")
    p.add_argument("--strict", action="store_true")
    args = p.parse_args()

    passes: list[dict[str, Any]] = []
    for spec in args.passes:
        if "=" in spec:
            name, outcome = spec.rsplit("=", 1)
            outcome = outcome.strip().lower() or "unknown"
        else:
            name, outcome = spec, "unknown"
        passes.append({"name": name.strip(), "outcome": outcome})

    merged_counts: dict[str, int] = {}
    outcomes_sources: list[str] = []
    seen_sources: set[str] = set()
    for rel in args.outcomes_json:
        path = Path(rel)
        if not path.exists():
            continue
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen_sources:
            continue
        seen_sources.add(key)
        outcomes_sources.append(str(path))
        obj = load_json(path)
        for k, v in count_outcomes(obj).items():
            merged_counts[k] = merged_counts.get(k, 0) + v

    mutants_out = [str(x) for x in args.mutants_out if Path(x).exists()]
    root = classify(args.job_result, passes, merged_counts)
    result = normalized_result(root, args.job_result, passes)
    data = {
        "schema_version": 2,
        "kind": "mutants_shard",
        "category": "gate.security.mutation",
        "priority": "P1",
        "event": args.event,
        "shard": args.shard,
        "shard_total": args.shard_total,
        "job_result": args.job_result,
        "result": result,
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
        f"**Result:** {result}",
        f"**Job result:** {args.job_result}",
        f"**Root cause class:** `{root}`",
    ]
    if args.diff_base:
        rows.append(f"**Diff base:** `{args.diff_base}`")
    if passes:
        rows.append("**Passes:** " + ", ".join(f"`{p['name']}`={p.get('outcome')}" for p in passes))
    if merged_counts:
        rows.append("**Outcome counts:** " + ", ".join(f"{k}={v}" for k, v in sorted(merged_counts.items())))
    else:
        rows.append("**Outcome counts:** unavailable (no parseable outcomes.json); use pass outcomes and `mutants.out`.")
    if result != "success" or root in ("infra_cancelled", "timeout", "survivor_or_unknown"):
        rows.append("")
        rows.append(
            "If cancelled/timeout/unknown: treat as **unknown fate** (not a proven survivor). Check job timeout (100m), per-mutant timeout (300s), and shard logs/artifacts."
        )
    rows.append("")
    append_summary("\n".join(rows))

    if args.strict:
        return 0 if result == "success" and root in ("clean", "no_mutants_or_unknown") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
