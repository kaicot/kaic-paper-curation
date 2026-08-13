"""Runtime policy v2 behavior and canonical child-argv propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESOLVER = ROOT / "pipeline/tools/resolve_runtime_policy.py"
JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]


class RuntimePolicyTests(unittest.TestCase):
    def invoke(
        self,
        config: JsonObject,
        *extra: str,
        environment: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], JsonObject]:
        """Run the real CLI against one isolated config and result path."""
        with tempfile.TemporaryDirectory(prefix="runtime-policy-") as directory:
            root = Path(directory)
            config_path = root / "config.json"
            output_path = root / "result.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            child_environment = os.environ.copy()
            child_environment.update(environment or {})
            result = subprocess.run(
                [sys.executable, str(RESOLVER), "--config", str(config_path), "--json-out", str(output_path), *extra],
                cwd=ROOT,
                env=child_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
            return result, payload

    def test_default_is_codex_when_paid_environment_is_present(self) -> None:
        # Given: a safe v2 config with no explicit mode and every paid-provider key set.
        environment = {
            "ANTHROPIC_API_KEY": "synthetic-anthropic",
            "OPENAI_API_KEY": "synthetic-openai",
            "GOOGLE_API_KEY": "synthetic-google",
            "GEMINI_API_KEY": "synthetic-gemini",
            "RESEND_API_KEY": "synthetic-resend",
            "CLOUDFLARE_API_TOKEN": "synthetic-cloudflare",
        }

        # When: the real resolver is invoked.
        result, payload = self.invoke({"schema_version": 2, "runtime": {}}, environment=environment)

        # Then: Codex is selected and every paid surface/counter is denied or zero.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "allowed")
        self.assertEqual(payload["mode"], "codex")
        self.assertIs(payload["allow_paid_api"], False)
        capabilities = payload["capabilities"]
        self.assertTrue(all(not row["allowed"] for name, row in capabilities.items() if name != "codex_generation"))
        self.assertEqual(payload["counters"], {"credential_reads": 0, "egress": 0, "provider_imports": 0, "writes": 0})

    def test_cli_mode_precedes_config_and_propagates_canonical_argv(self) -> None:
        # Given: config chooses codex while CLI explicitly chooses off.
        config: JsonObject = {"schema_version": 2, "runtime": {"llm_mode": "codex", "allow_paid_api": False}}

        # When: the CLI override is resolved.
        result, payload = self.invoke(config, "--llm-mode", "off")

        # Then: off wins and the canonical child argv carries the selected mode exactly once.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["mode"], "off")
        self.assertEqual(payload["canonical_child_argv"], ["--llm-mode", "off"])
        self.assertFalse(payload["capabilities"]["codex_generation"]["allowed"])

    def test_environment_mode_cannot_select_provider(self) -> None:
        # Given: only environment variables request legacy API mode/provider selection.
        environment = {"PAPER_CURATION_LLM_MODE": "api", "API_PROVIDER": "anthropic"}

        # When: policy resolves a config with no mode.
        result, payload = self.invoke({"schema_version": 2, "runtime": {}}, environment=environment)

        # Then: environment selection is ignored and the frozen default remains Codex.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["mode"], "codex")

    def test_unsafe_inputs_are_schema_valid_denials(self) -> None:
        cases: list[tuple[str, JsonObject, tuple[str, ...]]] = [
            ("legacy-cli", {"schema_version": 2, "runtime": {}}, ("--llm-mode", "api")),
            ("unknown-cli", {"schema_version": 2, "runtime": {}}, ("--llm-mode", "mystery")),
            ("paid-flag", {"schema_version": 2, "runtime": {"allow_paid_api": True}}, ()),
            ("provider-field", {"schema_version": 2, "runtime": {"api_provider": "anthropic"}}, ()),
            ("paid-ack", {"schema_version": 2, "runtime": {}}, ("--acknowledge-paid-api-cost",)),
        ]
        for name, config, extra in cases:
            with self.subTest(name=name):
                # Given/When: one forbidden policy input crosses the CLI boundary.
                result, payload = self.invoke(config, *extra)

                # Then: it fails closed with a typed JSON denial and zero side-effect counters.
                self.assertEqual(result.returncode, 2)
                self.assertEqual(payload["status"], "denied")
                self.assertEqual(payload["schema"], "runtime-policy-v2")
                self.assertEqual(payload["counters"], {"credential_reads": 0, "egress": 0, "provider_imports": 0, "writes": 0})

    def test_config_loader_exposes_safe_runtime_defaults(self) -> None:
        # Given: the worktree root is explicitly first on a child interpreter's import path.
        program = (
            "import json,sys;"
            f"sys.path.insert(0,{str(ROOT)!r});"
            "from pipeline.config_loader import get_runtime_policy;"
            "print(json.dumps(get_runtime_policy({'schema_version':2,'runtime':{}}),sort_keys=True))"
        )

        # When: the shared package module is imported and its accessor is called.
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        # Then: it returns the same immutable safe defaults.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"allow_paid_api": False, "llm_mode": "codex", "schema_version": 2},
        )

    def test_run_full_propagates_only_canonical_mode_argv(self) -> None:
        # Given: the real top-level parser receives an explicit safe policy override.
        program = (
            "import json,sys;"
            f"sys.path[:0]=[{str(ROOT / 'pipeline')!r},{str(ROOT)!r}];"
            "from pipeline.run_full import build_parser,build_update_force_cmd;"
            "args=build_parser().parse_args(['--topic','fixture','--mode','curate','--llm-mode','off']);"
            "print(json.dumps(build_update_force_cmd(args,'skip')))"
        )

        # When: run_full constructs the downstream process argv.
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        # Then: the child receives one explicit canonical mode pair and no paid acknowledgement/provider flag.
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)
        self.assertEqual(argv.count("--llm-mode"), 1)
        self.assertEqual(argv[argv.index("--llm-mode") + 1], "off")
        self.assertNotIn("--acknowledge-paid-api-cost", argv)
        self.assertNotIn("--api-provider", argv)


if __name__ == "__main__":
    unittest.main()
