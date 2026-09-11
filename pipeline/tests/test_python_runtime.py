#!/usr/bin/env python3
"""Runtime provisioning contract tests."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from pipeline import python_runtime


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "pipeline/tools/bootstrap_python_runtime.py"
LSP_BOOTSTRAP = ROOT / "pipeline/tools/bootstrap_lsp.py"
LSP_RUNNER = ROOT / "pipeline/tools/run_lsp_diagnostics.py"


class RuntimeContractTests(unittest.TestCase):
    def test_runtime_provisioners_and_frozen_locks_exist(self) -> None:
        # Given: the checked-out runtime provisioning surface.
        expected = (
            BOOTSTRAP,
            LSP_BOOTSTRAP,
            LSP_RUNNER,
            ROOT / "requirements-lock-py312.txt",
            ROOT / "requirements-lock-py312-full.txt",
            ROOT / "pipeline/python-runtime-policy-v1.json",
            ROOT / "pipeline/dev-tools/node-lock.json",
            ROOT / "pipeline/dev-tools/lsp-lock.json",
            ROOT / "pipeline/dev-tools/lsp-bridge.json",
        )

        # When: every declared runtime artifact is resolved.
        missing = [path.relative_to(ROOT).as_posix() for path in expected if not path.is_file()]

        # Then: no runtime component can fall back to an ambient tool.
        self.assertEqual(missing, [])

    def test_python_bootstrap_rejects_archive_hash_drift(self) -> None:
        # Given: a structurally valid but unapproved runtime archive.
        with tempfile.TemporaryDirectory(prefix="pc-runtime-") as raw_tmp:
            tmp = Path(raw_tmp)
            archive = tmp / "python.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("python.exe", b"not-approved")
                bundle.writestr("python312.zip", b"stdlib")
                bundle.writestr("python312._pth", b"python312.zip\n.\n")
            pip_wheel = tmp / "pip.whl"
            pip_wheel.write_bytes(b"not-a-wheel")
            requirements = tmp / "requirements.txt"
            requirements.write_text("", encoding="utf-8")

            # When: the bootstrap is invoked with the frozen archive contract.
            result = subprocess.run(
                [
                    sys.executable,
                    str(BOOTSTRAP),
                    "--archive",
                    str(archive),
                    "--target",
                    str(tmp / "runtime"),
                    "--pip-wheel",
                    str(pip_wheel),
                    "--requirements",
                    str(requirements),
                    "--json-out",
                    str(tmp / "result.json"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            # Then: it fails before publishing either the runtime or attestation.
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((tmp / "runtime").exists())
            self.assertFalse((tmp / "result.json").exists())

    def test_supported_policy_accepts_a_python_312_patch_release(self) -> None:
        # Given: an explicitly chosen CPython 3.12.13 candidate.
        with tempfile.TemporaryDirectory(prefix="pc-python-patch-") as raw_tmp:
            candidate = Path(raw_tmp) / "python.exe"
            candidate.write_bytes(b"candidate")
            policy = python_runtime.load_policy(ROOT)
            reported = {
                "implementation": "CPython",
                "version": [3, 12, 13],
                "executable": str(candidate.resolve()),
            }

            # When: the candidate identity is read.
            with patch.object(python_runtime, "_run_json", return_value=reported):
                version = python_runtime._require_supported_interpreter(candidate, policy)

            # Then: policy pins the supported minor, not the old 3.12.10 patch.
            self.assertEqual(version, "3.12.13")

    def test_bad_candidate_never_publishes_over_last_known_good_runtime(self) -> None:
        # Given: a 3.13 candidate and an already-published runtime pair.
        with tempfile.TemporaryDirectory(prefix="pc-python-bad-") as raw_tmp:
            tmp = Path(raw_tmp)
            candidate = tmp / "candidate.exe"
            candidate.write_bytes(b"candidate")
            target = tmp / "python312"
            target.mkdir()
            (target / "sentinel.txt").write_text("old", encoding="utf-8")
            attestation = tmp / "python312-resolved.json"
            attestation.write_text("old-attestation", encoding="utf-8")
            policy = python_runtime.load_policy(ROOT)
            reported = {
                "implementation": "CPython",
                "version": [3, 13, 0],
                "executable": str(candidate.resolve()),
            }

            # When: qualification rejects it before a staging or publish step.
            with patch.object(python_runtime, "_run_json", return_value=reported):
                with self.assertRaises(python_runtime.PythonRuntimeError):
                    python_runtime._require_supported_interpreter(candidate, policy)

            # Then: the known-good pair remains unchanged.
            self.assertEqual((target / "sentinel.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual(attestation.read_text(encoding="utf-8"), "old-attestation")

    def test_failed_pointer_promotion_keeps_active_runtime_unchanged(self) -> None:
        # Given: a separately staged venv record and the legacy active runtime.
        with tempfile.TemporaryDirectory(prefix="pc-python-pointer-") as raw_tmp:
            tmp = Path(raw_tmp)
            shutil.copytree(ROOT / "pipeline", tmp / "pipeline", ignore=shutil.ignore_patterns("__pycache__"))
            policy = python_runtime.load_policy(tmp)
            candidate_pointer, managed_root = python_runtime._candidate_paths(tmp, policy)
            candidate = managed_root / "cpython-3.12.13-fixture"
            (candidate / "Scripts").mkdir(parents=True)
            executable = candidate / "Scripts/python.exe"
            executable.write_bytes(b"candidate")
            attestation = tmp / ".omo/runtime/python312-resolved-fixture.json"
            attestation.parent.mkdir(parents=True)
            attestation.write_text("{}", encoding="utf-8")
            python_runtime._write_atomic_json(candidate_pointer, python_runtime._pointer_value(candidate, attestation, tmp))
            active = tmp / ".omo/runtime/python312-active.json"
            legacy = tmp / ".tools/python312"
            legacy.mkdir(parents=True)
            legacy_attestation = tmp / ".omo/runtime/python312-resolved.json"
            legacy_attestation.write_text("{}", encoding="utf-8")
            old_pointer = python_runtime._pointer_value(legacy, legacy_attestation, tmp)
            python_runtime._write_atomic_json(active, old_pointer)
            original_write = python_runtime._write_atomic_json

            def fail_active(path: Path, value: object) -> None:
                if str(path.resolve()).casefold() == str(active.resolve()).casefold():
                    raise OSError("simulated pointer write failure")
                original_write(path, value)  # type: ignore[arg-type]

            # When: final pointer replacement fails after candidate validation.
            verified = python_runtime.PythonRuntime(tmp, executable, candidate, attestation, "3.12.13")
            with patch.object(python_runtime, "validate_runtime_from_paths", return_value=verified), patch.object(python_runtime, "_write_atomic_json", side_effect=fail_active):
                with self.assertRaises(OSError):
                    python_runtime.promote_staged_candidate(tmp)

            # Then: active launches still resolve to the old pointer and the
            # candidate remains staged for a later retry.
            self.assertEqual(json.loads(active.read_text(encoding="utf-8")), old_pointer)
            self.assertTrue(candidate_pointer.exists())

    def test_qualification_lock_is_closed_and_hash_bearing(self) -> None:
        # Given: the future qualification lock, generated from existing direct pins.
        packages = python_runtime.locked_packages(ROOT / "requirements-lock-py312-full.txt")

        # Then: transitive identities are present; a future staged venv need not
        # depend on an ambient Python installation's package set.
        self.assertGreaterEqual(len(packages), 70)
        self.assertEqual(packages["openai"], "2.53.0")
        self.assertEqual(packages["adapters"], "1.3.0")
        self.assertEqual(packages["transformers"], "4.57.6")
        self.assertIn("pydantic", packages)
        self.assertIn("torch", packages)

    def test_qualified_runtime_uses_immutable_lock_snapshot(self) -> None:
        # Given: an attested runtime and a later edited checked-in full lock.
        with tempfile.TemporaryDirectory(prefix="pc-lock-snapshot-") as raw_tmp:
            tmp = Path(raw_tmp)
            shutil.copytree(ROOT / "pipeline", tmp / "pipeline", ignore=shutil.ignore_patterns("__pycache__"))
            policy = python_runtime.load_policy(tmp)
            lock_content = b"sample==1.0 --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            lock_digest = sha256(lock_content).hexdigest()
            snapshot = tmp / ".omo/runtime/locks" / f"sha256-{lock_digest}.txt"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_bytes(lock_content)
            # This is intentionally different: snapshot validation must not use it.
            (tmp / "requirements-lock-py312-full.txt").write_text("other==1.0 --hash=sha256:" + "b" * 64 + "\n", encoding="utf-8")
            attestation = {
                "schema_version": 2,
                "requirements_lock": snapshot.relative_to(tmp).as_posix(),
                "requirements_sha256": lock_digest,
                "locked_packages": {"sample": "1.0"},
            }

            # When: the historical lock binding is checked.
            python_runtime._validate_lock_identity(tmp, attestation, policy)

            # Then: later qualification-lock edits cannot invalidate the LKG.
            self.assertEqual(python_runtime.locked_packages(snapshot), {"sample": "1.0"})

    def test_node_and_lsp_locks_pin_sources_and_integrity(self) -> None:
        # Given: checked-in Node and language-server lock documents.
        node_path = ROOT / "pipeline/dev-tools/node-lock.json"
        lsp_path = ROOT / "pipeline/dev-tools/lsp-lock.json"
        self.assertTrue(node_path.is_file(), "Node lock must be checked in")
        self.assertTrue(lsp_path.is_file(), "LSP lock must be checked in")
        node = json.loads(node_path.read_text("utf-8"))
        lsp = json.loads(lsp_path.read_text("utf-8"))

        # When: the machine-consumed package identities are inspected.
        packages = {item["name"]: item for item in lsp["packages"]}

        # Then: every source and integrity is exact, never a version range/latest URL.
        self.assertEqual(node["version"], "v24.19.0")
        self.assertEqual(node["npm_version"], "11.17.0")
        self.assertEqual(packages["basedpyright"]["version"], "1.39.9")
        self.assertEqual(packages["@biomejs/biome"]["version"], "2.5.6")
        self.assertTrue(all(item["integrity"].startswith("sha512-") for item in packages.values()))
        self.assertEqual(len(node["archive_sha256"]), 64)

    def test_bridge_maps_python_and_javascript_to_one_engine_each(self) -> None:
        # Given: the checked-in LSP bridge.
        bridge_path = ROOT / "pipeline/dev-tools/lsp-bridge.json"
        self.assertTrue(bridge_path.is_file(), "LSP bridge must be checked in")
        bridge = json.loads(bridge_path.read_text("utf-8"))

        # When: its extension routing is parsed.
        mappings = bridge["extensions"]

        # Then: Python and JavaScript have one deterministic terminal engine.
        self.assertEqual(mappings[".py"], "basedpyright")
        self.assertEqual(mappings[".js"], "biome")
        self.assertEqual(set(bridge["engines"]), {"basedpyright", "biome"})


if __name__ == "__main__":
    unittest.main()
