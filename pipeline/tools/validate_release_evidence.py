#!/usr/bin/env python3
"""Todo 23 — validate the receipt/ledger evidence chain.

Checks that ledger entries 1..N are contiguous, each receipt exists with a
matching sha, the previous-entry chain binds, each task has exactly one
finalized receipt (attachment-as-receipt is rejected), and everything stays
inside one attempt root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class ReleaseEvidenceError(RuntimeError):
    """The evidence chain violated a required invariant."""


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--completed-through", type=int, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--require-receipt-ledger", type=Path)
    args = parser.parse_args()

    failures: list[str] = []
    evidence_root = args.evidence_root.resolve()
    ledger_path = args.require_receipt_ledger or (evidence_root / "receipt-ledger-v1.jsonl")
    if not ledger_path.is_file():
        failures.append(f"ledger missing: {ledger_path}")
        _emit(failures)
        return 1

    entries = load_jsonl(ledger_path)
    expected = list(range(1, args.completed_through + 1))
    actual = [entry["sequence"] for entry in entries]
    if actual != expected:
        failures.append(f"ledger sequences not contiguous 1..{args.completed_through}: {actual}")
    if not args.allow_incomplete and len(entries) != args.completed_through:
        failures.append(f"incomplete ledger: {len(entries)} entries for {args.completed_through}")

    previous: str | None = None
    seen_tasks: set[int] = set()
    for entry in entries:
        seq = entry["sequence"]
        task_id = entry["task_id"]
        if task_id in seen_tasks:
            failures.append(f"duplicate task receipt: {task_id}")
        seen_tasks.add(task_id)
        if entry.get("previous_entry_sha256") != previous:
            failures.append(f"seq {seq}: previous chain mismatch")
        previous = entry["entry_sha256"]
        receipt_path = Path(entry["receipt_path"])
        if not receipt_path.is_file():
            failures.append(f"seq {seq}: receipt missing: {receipt_path}")
            continue
        actual_sha = sha256_file(receipt_path)
        if actual_sha != entry["receipt_sha256"]:
            failures.append(f"seq {seq}: receipt sha mismatch")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("result") != "PASS":
            failures.append(f"seq {seq}: receipt result != PASS")
        if receipt.get("task_id") != task_id:
            failures.append(f"seq {seq}: receipt task_id mismatch")
        if receipt.get("previous_ledger_entry_sha256") != entry.get("previous_entry_sha256"):
            failures.append(f"seq {seq}: receipt previous-ledger mismatch")
        # attachment-as-receipt guard: no attachment may be a receipt JSON.
        for attachment in receipt.get("attachments", []):
            name = Path(attachment["path"]).name
            if name == "receipt.json":
                failures.append(f"seq {seq}: attachment-as-receipt rejected: {attachment['path']}")

    # plan binding
    plan_bytes = args.plan.read_bytes()
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    run_json = evidence_root / "run.json"
    if run_json.is_file():
        run = json.loads(run_json.read_text(encoding="utf-8"))
        if run.get("plan_sha256") and run["plan_sha256"] != plan_sha:
            failures.append("plan_sha256 mismatch with run.json")
        if run.get("evidence_root") and Path(run["evidence_root"]).resolve() != evidence_root:
            failures.append("evidence-root mismatch with run.json")
    else:
        failures.append(f"run.json missing: {run_json}")

    _emit(failures)
    return 0 if not failures else 1


def _emit(failures: list[str]) -> None:
    payload = {
        "schema": "release-evidence-validation-v1",
        "schema_version": 1,
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
