#!/usr/bin/env python3
"""Aggregate per-gate and mutant shard summaries into one CI triage report.

Reads JSON files from a directory (downloaded artifacts). Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P9": 9}
RESULT_BAD = {"failure", "cancelled", "timed_out", "timeout", "error"}


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


def load_jsons(root: Path) -> list[dict[str, Any]]:
    out = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict):
            obj.setdefault("_source", str(path))
            out.append(obj)
    return out


def gate_sort_key(g: dict[str, Any]) -> tuple[int, str, str]:
    return (
        PRIORITY_ORDER.get(str(g.get("priority", "P9")), 9),
        str(g.get("category", "")),
        str(g.get("job", "")),
    )


def normalize_gate(obj: dict[str, Any]) -> dict[str, Any] | None:
    kind = obj.get("kind")
    if kind == "gate":
        return {
            "priority": obj.get("priority", "P9"),
            "category": obj.get("category", "unknown"),
            "job": obj.get("job", obj.get("ci_job", "unknown")),
            "result": obj.get("result", "unknown"),
            "reason": obj.get("reason", ""),
            "evidence": obj.get("evidence", obj.get("_source", "")),
            "fix_first": obj.get("fix_first", ""),
            "details": {k: v for k, v in obj.items() if k not in {"_source"}},
        }
    if kind == "mutants_shard":
        result = obj.get("job_result", "unknown")
        root = obj.get("root_cause_class", "unknown")
        reason = f"shard {obj.get('shard')}/{obj.get('shard_total')}: {root}"
        counts = obj.get("outcome_counts") or {}
        if counts:
            reason += "; " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        fix = ""
        if root == "survivor":
            fix = "Add/strengthen tests or fix logic for survived mutants; inspect mutants.out for this shard."
        elif root in ("timeout", "infra_cancelled"):
            fix = "Unknown fate (not a proven survivor). Check shard timeout/cancellation and rerun; inspect shard logs/artifact."
        elif str(result).lower() in RESULT_BAD:
            fix = "Inspect shard artifact/logs; distinguish tool/infra failure from mutation result."
        return {
            "priority": "P1",
            "category": "gate.security.mutation",
            "job": f"mutants-shard-{obj.get('event','?')}-{obj.get('shard','?')}",
            "result": result,
            "reason": reason,
            "evidence": obj.get("artifact_name") or obj.get("_source", ""),
            "fix_first": fix,
            "details": obj,
        }
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--out-json", required=True)
    p.add_argument("--out-md", required=True)
    p.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    args = p.parse_args()

    objs = load_jsons(Path(args.input_dir))
    gates = []
    for obj in objs:
        g = normalize_gate(obj)
        if g:
            gates.append(g)
    gates.sort(key=gate_sort_key)

    failures = [g for g in gates if str(g.get("result", "")).lower() in RESULT_BAD]
    fix_first = []
    seen = set()
    for g in failures:
        cat = g["category"]
        if cat not in seen:
            seen.add(cat)
            fix_first.append(
                {
                    "priority": g["priority"],
                    "category": cat,
                    "reason": g.get("reason") or g.get("result"),
                    "evidence": g.get("evidence", ""),
                    "hint": g.get("fix_first", ""),
                }
            )

    report = {
        "schema_version": 1,
        "kind": "ci_triage",
        "event": args.event,
        "sha": os.environ.get("GITHUB_SHA", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "created_at": utc_now(),
        "overall_result": "failure" if failures else "success",
        "gate_count": len(gates),
        "failure_count": len(failures),
        "fix_first": fix_first,
        "gates": gates,
        "sources_loaded": len(objs),
    }

    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = ["# CI triage summary", "", f"- Event: `{args.event}`", f"- Overall: **{report['overall_result']}**", f"- Gates loaded: {len(gates)} (from {len(objs)} JSON files)", f"- Failures/cancellations: {len(failures)}", ""]
    if fix_first:
        lines += ["## Fix first", ""]
        for item in fix_first:
            hint = f" — {item['hint']}" if item.get("hint") else ""
            lines.append(f"- **{item['priority']} {item['category']}**: {item['reason']}{hint}")
        lines.append("")
    lines += ["## Gates (priority order)", "", "| Priority | Category | Job | Result | Reason | Evidence |", "|---|---|---|---|---|---|"]
    for g in gates:
        reason = str(g.get("reason", "")).replace("\n", " ").replace("|", "\\|")
        ev = str(g.get("evidence", "")).replace("|", "\\|")
        lines.append(f"| {g['priority']} | {g['category']} | {g['job']} | {g['result']} | {reason} | {ev} |")
    if not gates:
        lines += ["", "_No gate summary JSON files were found. Check artifact download patterns and per-job summary steps._"]
    lines.append("")
    md = "\n".join(lines)
    Path(args.out_md).write_text(md, encoding="utf-8")
    append_summary(md + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
