"""Fail-closed paid-provider boundary contracts."""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import cast
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.provider_inventory import InventoryError, JsonObject, load_object
from pipeline.runtime_policy import RuntimePolicyError, resolve_runtime_policy
from pipeline.tools import check_provider_inventory as boundary


class ProviderBoundaryTests(unittest.TestCase):
    """Boundary behavior against the real frozen manifest and scanner."""

    def checker(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "pipeline/tools/check_provider_inventory.py"),
                "--patterns",
                str(ROOT / "pipeline/provider-scan-patterns-v1.json"),
                "--manifest",
                str(ROOT / "pipeline/provider-entrypoints.json"),
                "--scanner-lock",
                str(ROOT / "pipeline/provider-scanner.lock.json"),
                "--scanner-attestation",
                str(Path(".omo/runtime/provider-scanner-resolved.json").resolve()),
                "--baseline",
                "fc49f2672dcbb4779fa36b31ea5eceb44c090503",
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": str(Path(sys.executable).parent),
                "PYTHONDONTWRITEBYTECODE": "1",
                "SYSTEMROOT": os.environ["SYSTEMROOT"],
            },
        )

    def test_scope_accepts_only_codex_or_off_and_rejects_paid_variants(self) -> None:
        self.assertEqual(resolve_runtime_policy({"schema_version": 2}).mode, "codex")
        self.assertEqual(
            resolve_runtime_policy({"schema_version": 2, "runtime": {"llm_mode": "off"}}).mode,
            "off",
        )
        forbidden: list[tuple[JsonObject, str | None, bool]] = [
            ({"schema_version": 2, "runtime": {"llm_mode": "api"}}, None, False),
            ({"schema_version": 2, "runtime": {"llm_mode": True}}, None, False),
            ({"schema_version": 2, "allow_paid_api": True}, None, False),
            ({"schema_version": 2, "allow_paid_api": 0}, None, False),
            ({"schema_version": 2, "allow_paid_api": None}, None, False),
            ({"schema_version": 2, "allow_paid_api": "false"}, None, False),
            ({"schema_version": 2, "runtime": {"allow_paid_api": 1}}, None, False),
            ({"schema_version": 2, "runtime": {"allow_paid_api": "no"}}, None, False),
            ({"schema_version": 2, "api_provider": "legacy"}, None, False),
            ({"schema_version": 2, "runtime": {"api_provider": "legacy"}}, None, False),
            ({"schema_version": 2}, "api", False),
            ({"schema_version": 2}, None, True),
        ]
        for config, cli_mode, acknowledged in forbidden:
            with self.subTest(config=config, cli_mode=cli_mode, acknowledged=acknowledged):
                with self.assertRaises(RuntimePolicyError):
                    _ = resolve_runtime_policy(
                        config,
                        cli_mode=cli_mode,
                        paid_acknowledged=acknowledged,
                    )
        poison_environment = {
            "PAPER_CURATION_LLM_MODE": "api",
            "ANTH" + "ROPIC_API_KEY": "poison",
            "OPEN" + "AI_API_KEY": "poison",
            "GOOGLE" + "_API_KEY": "poison",
            "OPEN" + "AI_BASE_URL": "poison",
            "API_" + "PROVIDER": "poison",
        }
        with patch.dict(os.environ, poison_environment, clear=False):
            self.assertEqual(resolve_runtime_policy({"schema_version": 2}).mode, "codex")

    def test_checker_requires_one_mode_and_valid_owner_scope(self) -> None:
        for arguments in (
            (),
            ("--allow-owned-baseline-violations", "--require-zero-unresolved"),
            ("--allow-owned-baseline-violations", "--owners", "8"),
            ("--require-owner-clean", "99"),
        ):
            with self.subTest(arguments=arguments):
                result = self.checker(*arguments)
                self.assertEqual(result.returncode, 2)

    def test_fingerprint_detects_duplicate_existing_shape(self) -> None:
        patterns = load_object(ROOT / "pipeline/provider-scan-patterns-v1.json")
        constructor = "Anth" + "ropic"
        one = f"client = {constructor}()\n".encode()
        two = f"client = {constructor}()\nother = {constructor}()\n".encode()
        self.assertNotEqual(
            boundary.finding_fingerprint("fixture.py", one, patterns),
            boundary.finding_fingerprint("fixture.py", two, patterns),
        )

    def test_paid_compat_is_metadata_only_and_poison_import_clean(self) -> None:
        module = importlib.import_module("pipeline.providers.paid_compat")
        exported = cast(tuple[str, ...], getattr(module, "__all__"))
        self.assertEqual(exported, ("PAID_PROVIDER_QUARANTINE",))
        quarantine = cast(object, getattr(module, "PAID_PROVIDER_QUARANTINE"))
        self.assertIsInstance(quarantine, MappingProxyType)
        self.assertFalse(callable(quarantine))
        namespace = cast(dict[str, object], vars(module))
        for public_name, value in namespace.items():
            if not public_name.startswith("_") and public_name != "annotations":
                self.assertFalse(callable(value), public_name)
        for forbidden in ("Anth" + "ropic", "Open" + "AI", "Generative" + "Model"):
            with self.assertRaises(AttributeError):
                _ = cast(object, getattr(module, forbidden))
        imported = boundary.poison_import_modules(
            ROOT,
            ("pipeline.providers.paid_compat",),
        )
        self.assertEqual(imported, ["pipeline.providers.paid_compat"])

    def test_exact_baseline_checker_imports_the_clean_default_set(self) -> None:
        result = self.checker(
            "--allow-owned-baseline-violations",
            "--owners",
            "9-15",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = cast(JsonObject, json.loads(result.stdout))
        self.assertEqual(summary["result"], "PASS")
        self.assertEqual(summary["unowned"], 0)
        self.assertEqual(
            summary["clean_default_modules"],
            list(boundary.CLEAN_DEFAULT_MODULES),
        )
        self.assertEqual(summary["poison_imported"], len(boundary.CLEAN_DEFAULT_MODULES) + 1)
        self.assertEqual(summary["quarantine_findings"], 1)

    def test_owner_clean_selection_covers_every_manifest_path(self) -> None:
        manifest = load_object(ROOT / "pipeline/provider-entrypoints.json")
        entrypoints = manifest.get("entrypoints")
        self.assertIsInstance(entrypoints, list)
        assert isinstance(entrypoints, list)
        for owner in range(9, 16):
            expected = sorted(
                path
                for row in entrypoints
                if isinstance(row, dict) and row.get("owner_todo") == owner
                if isinstance(path := row.get("path"), str)
            )
            self.assertEqual(boundary.paths_for_owner(manifest, owner), expected)

    def test_untracked_provider_shape_fails_and_is_removed(self) -> None:
        fixture = ROOT / "pipeline/_provider_boundary_unowned.py"
        provider = "anth" + "ropic"
        constructor = "Anth" + "ropic"
        fixture_source = (
            f"from {provider} import {constructor}\n"
            f"client = {constructor}()\n"
        )
        try:
            _ = fixture.write_text(fixture_source, encoding="utf-8")
            result = self.checker(
                "--allow-owned-baseline-violations",
                "--owners",
                "9-15",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("unowned", result.stderr.lower())
        finally:
            fixture.unlink(missing_ok=True)
        self.assertFalse(fixture.exists())

    def test_quarantine_validator_rejects_imports_calls_and_public_callables(self) -> None:
        provider = "anth" + "ropic"
        constructor = "Anth" + "ropic"
        bad_sources = [
            f"import {provider}\n",
            f"client = {constructor}()\n",
            "def public_factory():\n    return object()\n",
        ]
        for source in bad_sources:
            with self.subTest(source=source), self.assertRaises(InventoryError):
                boundary.validate_quarantine_source(source.encode("utf-8"))
        valid = (
            "from types import MappingProxyType\n"
            "PAID_PROVIDER_QUARANTINE = MappingProxyType({'name': 'disabled'})\n"
            "__all__ = ('PAID_PROVIDER_QUARANTINE',)\n"
            "def __getattr__(name):\n    raise AttributeError(name)\n"
        )
        boundary.validate_quarantine_source(valid.encode("utf-8"))

    def test_guard_module_ast_has_no_paid_import_or_constructor(self) -> None:
        source = (ROOT / "pipeline/tools/check_provider_inventory.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_modules = {
            "anth" + "ropic",
            "open" + "ai",
            "google." + "genai",
            "google." + "generativeai",
        }
        forbidden_calls = {"Anth" + "ropic", "Open" + "AI", "Generative" + "Model"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(alias.name not in forbidden_modules for alias in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, forbidden_modules)
            elif isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else None
                self.assertNotIn(name, forbidden_calls)


if __name__ == "__main__":
    _ = unittest.main()
