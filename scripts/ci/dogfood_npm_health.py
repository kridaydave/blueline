#!/usr/bin/env python3
"""npm dogfood health gate (P3) and risk reporting (P4). Preserves gate semantics."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

EXPECTED = {
    "@kridaydave/blueline-cli", "blueline-cli",
    "@kridaydave/binary-darwin-arm64", "@kridaydave/binary-darwin-x64",
    "@kridaydave/binary-linux-arm64-gnu", "@kridaydave/binary-linux-arm64-musl",
    "@kridaydave/binary-linux-x64-musl",
    "@kridaydave/binary-linux-x64-gnu", "@kridaydave/binary-win32-arm64",
    "@kridaydave/binary-win32-x64",
}
RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "BLOCK": 3}


def add_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def fail(msg: str, scan_outcome: str, scan_exit: str, report_path: str, lockfile_changed: bool) -> None:
    print(f"::error title=P3 gate.dogfood.health::{msg}")
    add_summary(
        f"## P3 gate.dogfood.health — npm dogfood\n\n**FAIL:** {msg}\n\n"
        f"- scan outcome: `{scan_outcome}` (exit `{scan_exit}`)\n"
        f"- report path: `{report_path}`\n- lockfile changed: `{lockfile_changed}`\n"
    )
    raise SystemExit(1)


def main() -> int:
    lockfile_changed = os.environ.get("LOCKFILE_CHANGED") == "true"
    scan_outcome = os.environ.get("SCAN_OUTCOME", "unknown")
    scan_exit = os.environ.get("SCAN_EXIT_CODE", "")
    report_path = os.environ.get("REPORT_PATH") or "blueline-dogfood.json"
    Path("artifacts").mkdir(exist_ok=True)
    report = Path(report_path)
    if not report.exists():
        fail(f"scan produced no report file (scan outcome={scan_outcome}, exit={scan_exit}); scanner health cannot be verified", scan_outcome, scan_exit, report_path, lockfile_changed)
    try:
        r = json.loads(report.read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"scan report is not valid JSON ({e}); scan outcome={scan_outcome}", scan_outcome, scan_exit, report_path, lockfile_changed)

    names = {i["name"] for i in r.get("items", [])}
    unknown = names - EXPECTED
    if unknown:
        fail(f"scanner evaluated unknown packages: {sorted(unknown)}", scan_outcome, scan_exit, report_path, lockfile_changed)
    bands = [i["verdict"]["band"] for i in r.get("items", [])]
    unknown_bands = sorted({b for b in bands if b not in RANK})
    if unknown_bands:
        fail(f"scanner emitted unknown bands: {unknown_bands}", scan_outcome, scan_exit, report_path, lockfile_changed)
    if r.get("total_evaluated", 0) == 0 and lockfile_changed:
        fail("package-lock.json changed but the scan evaluated nothing: scanner reviewed no shipped package while the delta was non-empty", scan_outcome, scan_exit, report_path, lockfile_changed)
    if bands:
        for item in r["items"]:
            if not item.get("name") or not item.get("new_version"):
                fail(f"report carries an item with no name or version: {item}", scan_outcome, scan_exit, report_path, lockfile_changed)
        derived = RANK[max(bands, key=lambda b: RANK[b])]
        if derived != RANK.get(r.get("max_band")):
            fail(f"max_band {r.get('max_band')} disagrees with reported bands {bands}", scan_outcome, scan_exit, report_path, lockfile_changed)

    findings = [(i["name"], i["new_version"], i["verdict"]["band"]) for i in r.get("items", []) if RANK[i["verdict"]["band"]] >= RANK["HIGH"]]
    if r.get("total_evaluated", 0) == 0:
        msg = f"scanner healthy (scan outcome={scan_outcome}, exit={scan_exit}): lockfile unchanged, nothing to evaluate"
    else:
        msg = f"scanner healthy (scan outcome={scan_outcome}, exit={scan_exit}): evaluated {r.get('total_evaluated')}, unchanged {r.get('unchanged_count')}, max band {r.get('max_band')}"
    print(f"::notice title=P3 gate.dogfood.health::{msg}")
    print(msg)
    for item in r.get("items", []):
        print(f"  {item['name']}@{item['new_version']}: {item['verdict']['band']}")
    add_summary(f"## P3 gate.dogfood.health — npm dogfood\n\n**PASS:** {msg}\n\n- lockfile changed: `{lockfile_changed}`\n")
    if findings:
        add_summary("## P4 gate.dogfood.risk — findings (not gating)\n\nScanner is healthy. HIGH/BLOCK findings are reported for investigation and may be expected on first-party binary bumps.\n\n| package | version | band |\n| --- | --- | --- |\n")
        for name, version, band in findings:
            add_summary(f"| {name} | {version} | {band} |")
        add_summary("")
        Path("artifacts/gate-dogfood-risk.json").write_text(json.dumps({
            "schema_version": 1, "kind": "gate", "priority": "P4", "category": "gate.dogfood.risk",
            "job": "dogfood", "result": "success", "reason": f"{len(findings)} HIGH/BLOCK finding(s); scanner healthy (not gating)",
            "evidence": report_path, "fix_first": "Review findings; expected on first-party binary bumps, but investigate unexpected packages/changes.",
            "details": {"findings": [{"name": n, "version": v, "band": b} for n, v, b in findings], "scan_outcome": scan_outcome, "scan_exit_code": scan_exit},
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"::notice title=P4 gate.dogfood.risk::{len(findings)} HIGH/BLOCK finding(s) recorded; scanner healthy (not gating)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
