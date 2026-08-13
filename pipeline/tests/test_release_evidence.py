"""Todo 23 — release evidence tests (RED first).

Prove the receipt/ledger chain validator (contiguity, sha binding, chain
binding, one-receipt-per-task, attachment-as-receipt rejection), the
final-review command runner envelope contract (quiet exit-0 still nonempty,
ID selectors), the attachment assembler producer mapping (filler rejected),
and the F1/F2 cleanup state comparison.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
VALIDATE = REPO_ROOT / "pipeline/tools/validate_release_evidence.py"
RUNNER = REPO_ROOT / "pipeline/tools/run_final_review_command.py"
ASSEMBLER = REPO_ROOT / "pipeline/tools/assemble_final_review_attachment.py"
CLEANUP = REPO_ROOT / "pipeline/tools/verify_final_review_cleanup.py"


def make_receipt(task_id: int, previous: str | None, root: Path) -> dict:
    receipt = {
        "schema_version": 1,
        "task_id": task_id,
        "result": "PASS",
        "previous_ledger_entry_sha256": previous,
        "attachments": [],
    }
    path = root / f"task-{task_id}" / "receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {"receipt_path": str(path), "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def build_evidence_root(root: Path, count: int, *, tamper: str | None = None) -> None:
    plan = (root / "approved-plan.md")
    plan.write_text("plan", encoding="utf-8")
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    (root / "run.json").write_text(
        json.dumps({"plan_sha256": plan_sha, "evidence_root": str(root.resolve()), "attempt_id": "x"}),
        encoding="utf-8",
    )
    previous = None
    for task_id in range(1, count + 1):
        info = make_receipt(task_id, previous, root)
        row = {
            "sequence": task_id,
            "task_id": task_id,
            "receipt_path": info["receipt_path"],
            "receipt_sha256": info["receipt_sha256"],
            "ownership_lease_sha256": "b" * 64,
            "previous_entry_sha256": previous,
            "attempt_id": "x",
            "schema_version": 1,
        }
        canon = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        row["entry_sha256"] = hashlib.sha256(canon).hexdigest()
        previous = row["entry_sha256"]
        if tamper == "chain" and task_id == 1:
            row["entry_sha256"] = "0" * 64
        if tamper == "sha" and task_id == count:
            row["receipt_sha256"] = "0" * 64
        with (root / "receipt-ledger-v1.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def run_validator(root: Path, count: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            PY, str(VALIDATE),
            "--plan", str(root / "approved-plan.md"),
            "--evidence-root", str(root),
            "--completed-through", str(count),
            "--allow-incomplete",
            "--require-receipt-ledger", str(root / "receipt-ledger-v1.jsonl"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


class ReleaseEvidenceChainTests(unittest.TestCase):
    def test_chain_1_to_22_validates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            build_evidence_root(root, 22)
            result = run_validator(root, 22)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["result"], "PASS")

    def test_tampered_chain_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            build_evidence_root(root, 22, tamper="chain")
            result = run_validator(root, 22)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["result"], "FAIL")

    def test_tampered_receipt_sha_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            build_evidence_root(root, 22, tamper="sha")
            result = run_validator(root, 22)
            self.assertNotEqual(result.returncode, 0)

    def test_attachment_as_receipt_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            build_evidence_root(root, 22)
            # Rewrite task-1 receipt to carry an attachment named receipt.json.
            receipt_path = root / "task-1" / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["attachments"] = [{"path": str(root / "task-1" / "attachments" / "receipt.json")}]
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            # Recompute the ledger row sha for task 1.
            lines = (root / "receipt-ledger-v1.jsonl").read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[0])
            row["receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            lines[0] = json.dumps(row, sort_keys=True)
            (root / "receipt-ledger-v1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = run_validator(root, 22)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(any("attachment-as-receipt" in f for f in json.loads(result.stdout)["failures"]))


class FinalReviewRunnerTests(unittest.TestCase):
    def test_quiet_exit_zero_command_yields_nonempty_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = subprocess.run(
                [
                    PY, str(RUNNER),
                    "--verifier", "F1",
                    "--command-id", "evidence_validate",
                    "--envelope-out", str(root / "env.json"),
                    "--stdout-out", str(root / "out.bin"),
                    "--stderr-out", str(root / "err.bin"),
                    "--", PY, "-c", "import sys; sys.exit(0)",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            envelope = json.loads((root / "env.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["command_id"], "evidence_validate")
            self.assertEqual(envelope["exit_code"], 0)
            self.assertEqual(envelope["stdout_size"], 0)
            self.assertGreater(len(envelope["stdout_sha256"]), 0)

    def test_unknown_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = subprocess.run(
                [
                    PY, str(RUNNER),
                    "--verifier", "F1",
                    "--command-id", "invented_filler",
                    "--envelope-out", str(root / "env.json"),
                    "--stdout-out", str(root / "out.bin"),
                    "--stderr-out", str(root / "err.bin"),
                    "--", PY, "-c", "pass",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_negative_requires_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = subprocess.run(
                [
                    PY, str(RUNNER),
                    "--verifier", "F1",
                    "--negative-id", "ledger_tamper",
                    "--envelope-out", str(root / "env.json"),
                    "--stdout-out", str(root / "out.bin"),
                    "--stderr-out", str(root / "err.bin"),
                    "--", PY, "-c", "import sys; sys.exit(7)",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((root / "env.json").read_text(encoding="utf-8"))["exit_code"], 7)

    def test_exactly_one_selector_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = subprocess.run(
                [
                    PY, str(RUNNER),
                    "--verifier", "F1",
                    "--envelope-out", str(root / "env.json"),
                    "--stdout-out", str(root / "out.bin"),
                    "--stderr-out", str(root / "err.bin"),
                    "--", PY, "-c", "pass",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)


class AttachmentAssemblerTests(unittest.TestCase):
    def _envelope(self, root: Path, command_id: str, index: int) -> Path:
        path = root / f"env-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "command_id": command_id,
                    "argv": [PY, "-c", "pass"],
                    "cwd": str(REPO_ROOT),
                    "started_at_utc": "2026-08-07T00:00:00.000000Z",
                    "ended_at_utc": "2026-08-07T00:00:00.100000Z",
                    "exit_code": 0,
                    "stdout_size": 0,
                    "stdout_sha256": "0" * 64,
                    "stderr_size": 0,
                    "stderr_sha256": "0" * 64,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def test_declared_producer_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = self._envelope(root, "release_tests", 1)
            result = subprocess.run(
                [
                    PY, str(ASSEMBLER),
                    "--verifier", "F1",
                    "--attachment-id", "tests",
                    "--out", str(root / "attach.json"),
                    "--envelope", str(env),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((root / "attach.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["attachment_id"], "tests")
            self.assertEqual(manifest["envelope_count"], 1)
            self.assertTrue(manifest["constituents"])

    def test_filler_producer_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = self._envelope(root, "diff_check", 1)
            result = subprocess.run(
                [
                    PY, str(ASSEMBLER),
                    "--verifier", "F1",
                    "--attachment-id", "tests",
                    "--out", str(root / "attach.json"),
                    "--envelope", str(env),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("filler rejected", result.stderr)

    def test_unknown_attachment_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = self._envelope(root, "release_tests", 1)
            result = subprocess.run(
                [
                    PY, str(ASSEMBLER),
                    "--verifier", "F1",
                    "--attachment-id", "invented",
                    "--out", str(root / "attach.json"),
                    "--envelope", str(env),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)


class CleanupVerifierTests(unittest.TestCase):
    def _state(self, root: Path, name: str, payload: dict) -> Path:
        path = root / f"{name}.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def test_f1_identical_protected_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence = {"protected_evidence": [{"path": "p", "file_id": "0x1", "size": 5, "sha256": "a" * 64}]}
            before = self._state(root, "before", evidence)
            after = self._state(root, "after", evidence)
            result = subprocess.run(
                [
                    PY, str(CLEANUP),
                    "--verifier", "F1", "--kind", "evidence_preserved",
                    "--before", str(before), "--after", str(after),
                    "--json-out", str(root / "cleanup.json"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((root / "cleanup.json").read_text(encoding="utf-8"))["status"], "PASS")

    def test_f1_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            before = self._state(
                root, "before",
                {"protected_evidence": [{"path": "p", "file_id": "0x1", "size": 5, "sha256": "a" * 64}]},
            )
            after = self._state(
                root, "after",
                {"protected_evidence": [{"path": "p", "file_id": "0x1", "size": 6, "sha256": "b" * 64}]},
            )
            result = subprocess.run(
                [
                    PY, str(CLEANUP),
                    "--verifier", "F1", "--kind", "evidence_preserved",
                    "--before", str(before), "--after", str(after),
                    "--json-out", str(root / "cleanup.json"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_f2_registered_temp_still_exists_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            before = self._state(
                root, "before",
                {"registered_temp_paths": [{"path": "t", "exists": True}]},
            )
            after = self._state(
                root, "after",
                {"registered_temp_paths": [{"path": "t", "exists": True}]},
            )
            result = subprocess.run(
                [
                    PY, str(CLEANUP),
                    "--verifier", "F2", "--kind", "tool_temp_removed",
                    "--before", str(before), "--after", str(after),
                    "--json-out", str(root / "cleanup.json"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
