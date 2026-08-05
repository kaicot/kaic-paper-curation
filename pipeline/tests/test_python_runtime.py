#!/usr/bin/env python3
"""Runtime provisioning contract tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from hashlib import sha256
from pathlib import Path


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
