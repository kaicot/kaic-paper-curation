"""Frozen provider inventory and locked Acorn scanner behavior."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASELINE = "fc49f2672dcbb4779fa36b31ea5eceb44c090503"
BOOTSTRAP = ROOT / "pipeline/tools/bootstrap_provider_scanner.py"
CHECKER = ROOT / "pipeline/tools/check_provider_inventory.py"
PATTERNS = ROOT / "pipeline/provider-scan-patterns-v1.json"
MANIFEST = ROOT / "pipeline/provider-entrypoints.json"
SCANNER_LOCK = ROOT / "pipeline/provider-scanner.lock.json"
NODE_ATTESTATION = ROOT / ".omo/runtime/node-resolved.json"


class ProviderInventoryTests(unittest.TestCase):
    def checker(self, *extra: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        child_environment = os.environ.copy()
        child_environment.update(environment or {})
        return subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--patterns",
                str(PATTERNS),
                "--manifest",
                str(MANIFEST),
                "--scanner-lock",
                str(SCANNER_LOCK),
                "--baseline",
                BASELINE,
                "--allow-owned-baseline-violations",
                "--owners",
                "9-15",
                *extra,
            ],
            cwd=ROOT,
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_manifest_freezes_all_lexical_baseline_files(self) -> None:
        # Given: the checked provider manifest generated from the immutable baseline.
        self.assertTrue(MANIFEST.is_file(), "provider manifest must be implemented")
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))

        # When: its frozen rows are inspected.
        rows = payload["entrypoints"]

        # Then: all 51 lexical seeds are normalized, blob-bound, reasoned, and uniquely owned.
        self.assertEqual(payload["baseline"]["lexical_file_count"], 51)
        self.assertEqual(len({row["path"] for row in rows if "lexical" in row["discovery_reasons"]}), 51)
        self.assertEqual([row["path"] for row in rows], sorted(row["path"] for row in rows))
        for row in rows:
            self.assertNotIn("\\", row["path"])
            self.assertRegex(row["baseline_blob_sha"], r"^[0-9a-f]{40}$")
            self.assertTrue(row["discovery_reasons"])
            self.assertIn(row["owner_todo"], range(9, 16))
            self.assertIn(row["disposition"], {"default-denied", "quarantine"})

    def test_locked_scanner_ignores_poisoned_ambient_node_path(self) -> None:
        # Given: NODE_PATH contains a fake ambient Acorn module with an observable side effect.
        with tempfile.TemporaryDirectory(prefix="ambient-acorn-") as directory:
            poison = Path(directory)
            sentinel = poison / "ambient-used.txt"
            module_dir = poison / "acorn"
            module_dir.mkdir()
            (module_dir / "package.json").write_text('{"type":"module","main":"index.mjs"}', encoding="utf-8")
            (module_dir / "index.mjs").write_text(
                f"import {{writeFileSync}} from 'node:fs';writeFileSync({json.dumps(str(sentinel))},'used');export const parse=()=>({{}});",
                encoding="utf-8",
            )

            # When: the bootstrap and exact inventory checker execute with that poisoned path.
            bootstrap = subprocess.run(
                [sys.executable, str(BOOTSTRAP), "--node-attestation", str(NODE_ATTESTATION), "--scanner-lock", str(SCANNER_LOCK)],
                cwd=ROOT,
                env={**os.environ, "NODE_PATH": str(poison)},
                capture_output=True,
                text=True,
                check=False,
            )
            checked = self.checker(environment={"NODE_PATH": str(poison)})

            # Then: locked Acorn is accepted and the ambient module is never evaluated.
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertFalse(sentinel.exists())

    def test_tool_hash_and_manifest_shape_drift_fail(self) -> None:
        # Given: disposable copies of the local scanner attestation and provider manifest.
        attestation = ROOT / ".omo/runtime/provider-scanner-resolved.json"
        self.assertTrue(attestation.is_file(), "scanner bootstrap must run before drift test")
        with tempfile.TemporaryDirectory(prefix="provider-drift-") as directory:
            temp = Path(directory)
            bad_attestation = temp / "scanner.json"
            attested = json.loads(attestation.read_text(encoding="utf-8"))
            attested["acorn_sha256"] = "0" * 64
            bad_attestation.write_text(json.dumps(attested), encoding="utf-8")
            bad_manifest = temp / "manifest.json"
            manifested = json.loads(MANIFEST.read_text(encoding="utf-8"))
            manifested["computed_shape_sha256"] = "0" * 64
            bad_manifest.write_text(json.dumps(manifested), encoding="utf-8")

            # When: each drifted input is checked independently.
            tool_result = self.checker("--scanner-attestation", str(bad_attestation))
            shape_result = self.checker("--manifest", str(bad_manifest))

            # Then: both checks fail closed.
            self.assertEqual(tool_result.returncode, 2)
            self.assertEqual(shape_result.returncode, 2)

    def test_unowned_worktree_provider_shape_fails_and_cleans_up(self) -> None:
        # Given: a unique untracked Python entrypoint constructs a paid provider directly.
        fixture = ROOT / "pipeline" / f"_task3_unowned_{uuid.uuid4().hex}.py"
        fixture.write_text("from anthropic import Anthropic\nclient = Anthropic()\n", encoding="utf-8")
        try:
            # When: the current worktree is scanned separately from the baseline.
            result = self.checker()

            # Then: the unowned/new shape is rejected.
            self.assertEqual(result.returncode, 2)
            self.assertIn("unowned", result.stderr.lower())
        finally:
            fixture.unlink(missing_ok=True)
        self.assertFalse(fixture.exists())


if __name__ == "__main__":
    unittest.main()
