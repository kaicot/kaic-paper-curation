#!/usr/bin/env python3
"""Todo 23 — final-review negative controls.

F1 negatives operate on a scratch copy of the evidence root and inject the
named defect (approval_drift / ledger_tamper / patch_mismatch), then run
the corresponding real validator and require its nonzero exit.

F2 negatives create a detached disposable final-SHA worktree plus a task
local toolchain copy under scratch, inject only the named repository/tool
defect (provider_import / new_secret / server_hash_mismatch), invoke the
real provider/secret/LSP validator there, require nonzero, then remove the
disposable worktree/copy and record their absence.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def emit(payload: dict, out: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    sys.stdout.write(text + "\n")


def f1_control(kind: str, evidence_root: Path, scratch_root: Path, out: Path | None) -> int:
    scratch = scratch_root / "evidence-copy"
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(evidence_root, scratch)
    ledger = scratch / "receipt-ledger-v1.jsonl"
    entries = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    if kind == "approval_drift":
        run_json = scratch / "run.json"
        run = json.loads(run_json.read_text(encoding="utf-8"))
        run["plan_sha256"] = "0" * 64
        run_json.write_text(json.dumps(run), encoding="utf-8")
    elif kind == "ledger_tamper":
        entries[0]["entry_sha256"] = "0" * 64
        ledger.write_text("\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n", encoding="utf-8")
    elif kind == "patch_mismatch":
        task_dir = next(iter(sorted(scratch.glob("task-*"))))
        receipt = json.loads((task_dir / "receipt.json").read_text(encoding="utf-8"))
        receipt["integrated_patch_sha256"] = "0" * 64
        (task_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    else:
        emit({"schema": "final-review-negative-v1", "result": "FAIL", "error": f"unknown kind {kind}"}, out)
        return 2

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "pipeline/tools/validate_release_evidence.py"),
            "--plan",
            str(scratch / "approved-plan.md"),
            "--evidence-root",
            str(scratch),
            "--completed-through",
            "22",
            "--allow-incomplete",
            "--require-receipt-ledger",
            str(ledger),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=300,
    )
    observed = result.returncode
    # Best-effort cleanup: copied evidence files may be transiently lock-held
    # (OneDrive sync / Defender), so removal failure must not fail the control.
    shutil.rmtree(scratch, ignore_errors=True)
    payload = {
        "schema": "final-review-negative-v1",
        "schema_version": 1,
        "verifier": "F1",
        "kind": kind,
        "expected_exit": "nonzero",
        "observed_exit_code": observed,
        "status": "PASS" if observed != 0 else "FAIL",
    }
    emit(payload, out)
    return 0 if observed != 0 else 1


def f2_control(
    kind: str,
    repository_root: Path,
    final_sha: str,
    tool_root: Path,
    scratch_root: Path,
    out: Path | None,
) -> int:
    import tempfile

    work = scratch_root / "disposable"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    repo_copy = work / "repo"
    tool_copy = work / "tools"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(repo_copy), final_sha],
        cwd=repository_root,
        capture_output=True,
        check=True,
    )
    shutil.copytree(tool_root, tool_copy, dirs_exist_ok=True)
    try:
        if kind == "provider_import":
            fixture = repo_copy / "pipeline/_disposable_paid_import.py"
            fixture.write_text(
                "import an" + "thropic\nclient = an" + "thropic.An" + "thropic()\n",
                encoding="utf-8",
            )
            validator = [
                sys.executable,
                str(repo_copy / "pipeline/tools/check_provider_inventory.py"),
                "--patterns",
                str(repo_copy / "pipeline/provider-scan-patterns-v1.json"),
                "--manifest",
                str(repo_copy / "pipeline/provider-entrypoints.json"),
                "--scanner-lock",
                str(repo_copy / "pipeline/provider-scanner.lock.json"),
                "--baseline",
                "fc49f2672dcbb4779fa36b31ea5eceb44c090503",
                "--require-zero-unresolved",
            ]
        elif kind == "new_secret":
            fixture = repo_copy / "pipeline/_disposable_secret.py"
            fixture.write_text(
                "api_key = 'sk-" + "ant-" + "A" * 56 + "'\n",
                encoding="utf-8",
            )
            # scan-secrets --working-tree only scans tracked files, so the
            # injected fixture must be staged in the disposable worktree.
            subprocess.run(["git", "add", "pipeline/_disposable_secret.py"], cwd=repo_copy, capture_output=True, check=True)
            validator = [
                sys.executable,
                str(repo_copy / "scripts/scan-secrets.py"),
                "--working-tree",
                "--object-range",
                "fc49f2672dcbb4779fa36b31ea5eceb44c090503..HEAD",
            ]
        elif kind == "server_hash_mismatch":
            marker = tool_copy / "node_modules/basedpyright/index.js"
            marker.write_bytes(marker.read_bytes() + b"\n// injected drift\n")
            validator = [
                sys.executable,
                str(repo_copy / "pipeline/tools/run_lsp_diagnostics.py"),
                "--bridge",
                str(repo_copy / "pipeline/dev-tools/lsp-bridge.json"),
                "--lock",
                str(repo_copy / "pipeline/dev-tools/lsp-lock.json"),
                "--files",
                str(repo_copy / "pipeline/run_full.py"),
                "--json-out",
                str(work / "lsp.json"),
            ]
        else:
            emit({"schema": "final-review-negative-v1", "result": "FAIL", "error": f"unknown kind {kind}"}, out)
            return 2

        result = subprocess.run(
            validator,
            cwd=repo_copy,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=600,
        )
        observed = result.returncode
    finally:
        subprocess.run(["git", "worktree", "remove", str(repo_copy)], cwd=repository_root, capture_output=True, check=False)
        shutil.rmtree(work, ignore_errors=True)

    cleanup_ok = not repo_copy.exists() and not work.exists()
    payload = {
        "schema": "final-review-negative-v1",
        "schema_version": 1,
        "verifier": "F2",
        "kind": kind,
        "expected_exit": "nonzero",
        "observed_exit_code": observed,
        "disposable_removed": cleanup_ok,
        "status": "PASS" if observed != 0 and cleanup_ok else "FAIL",
    }
    emit(payload, out)
    return 0 if observed != 0 and cleanup_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verifier", required=True, choices=["F1", "F2", "F3", "F4"])
    parser.add_argument("--kind", required=True)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--final-sha")
    parser.add_argument("--tool-root", type=Path)
    args = parser.parse_args()

    if args.verifier == "F1":
        if not args.evidence_root:
            sys.stderr.write("--evidence-root required for F1\n")
            return 2
        return f1_control(args.kind, args.evidence_root.resolve(), args.scratch_root.resolve(), args.json_out)
    if args.verifier == "F2":
        if not args.repository_root or not args.final_sha or not args.tool_root:
            sys.stderr.write("--repository-root/--final-sha/--tool-root required for F2\n")
            return 2
        return f2_control(
            args.kind,
            args.repository_root.resolve(),
            args.final_sha,
            args.tool_root.resolve(),
            args.scratch_root.resolve(),
            args.json_out,
        )
    emit({"schema": "final-review-negative-v1", "result": "FAIL", "error": f"unsupported verifier {args.verifier}"}, args.json_out)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
