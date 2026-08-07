"""Todo 22 — documented-command validator tests.

RED first: assert the validator contract (tagged fence parsing, shell
rejection, placeholder replacement, help/dry-run modes, safe config
defaults, shipped-only flags, secret scan) before the docs are updated.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "pipeline" / "tools" / "validate_documented_commands.py"
PY = sys.executable


def run_validator(paths: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    argv = [PY, str(TOOL), "--paths", *paths]
    return subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=300,
    )


def write_doc(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.write_text(content, encoding="utf-8")
    return path


class TokenizerContractTests(unittest.TestCase):
    """Prove the validator rejects shell and honors the token contract."""

    def test_tagged_blocks_are_parsed_and_untagged_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = write_doc(
                root,
                "sample.md",
                "# Docs\n\n"
                "```bash paper-curation-command\n"
                "PYTHONUTF8=1 python pipeline/run_full.py --topic qa_fixture --dry-run\n"
                "```\n\n"
                "```bash\n"
                "this is not a tagged command\n"
                "```\n",
            )
            result = run_validator([str(doc)])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            rows = payload["rows"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], str(doc))
            self.assertEqual(rows[0]["mode"], "dry-run")
            self.assertEqual(rows[0]["exit_code"], 0)
            self.assertEqual(rows[0]["result"], "pass")

    def test_help_mode_appended_when_no_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = write_doc(
                root,
                "help.md",
                "```bash paper-curation-command\n"
                "python pipeline/run_full.py --topic qa_fixture\n"
                "```\n",
            )
            result = run_validator([str(doc)])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row = json.loads(result.stdout)["rows"][0]
            self.assertEqual(row["mode"], "help")
            self.assertTrue(row["argv"][-1] == "--help" or "--help" in row["argv"])

    def test_shell_syntax_rejected(self) -> None:
        for bad in (
            "python pipeline/run_full.py --topic qa_fixture --dry-run | grep x",
            "python pipeline/run_full.py --topic qa_fixture && echo hi",
            "python pipeline/run_full.py --topic qa_fixture --dry-run ; rm -rf /",
            "python pipeline/run_full.py --topic qa_fixture > /tmp/out.txt",
            "python pipeline/run_full.py --topic `id` --dry-run",
            "python pipeline/run_full.py --topic qa --dry-run $(whoami)",
            "python pipeline/run_full.py --topic qa_* --dry-run",
        ):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    doc = write_doc(root, "bad.md", f"```bash paper-curation-command\n{bad}\n```\n")
                    result = run_validator([str(doc)])
                    self.assertNotEqual(result.returncode, 0, bad)

    def test_paid_env_assignment_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = write_doc(
                root,
                "env.md",
                "```bash paper-curation-command\n"
                + "ANTH" + "ROPIC_API_KEY=sk-fake python pipeline/run_full.py --topic qa --dry-run\n"
                + "```\n",
            )
            result = run_validator([str(doc)])
            self.assertNotEqual(result.returncode, 0)

    def test_temp_placeholder_replaced_with_unique_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = write_doc(
                root,
                "temp.md",
                "```bash paper-curation-command\n"
                "python pipeline/query_search_index.py --topic qa_fixture --query alpha --mode bm25 --json --out <temp>/result.json\n"
                "```\n",
            )
            result = run_validator([str(doc)])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row = json.loads(result.stdout)["rows"][0]
            self.assertTrue(any("<temp" in tok for tok in row["argv"]) is False)

    def test_config_example_resolves_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_doc(root, "config.example.json", (REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))
            write_doc(root, "empty.md", "no commands here\n")
            result = run_validator([str(root / "empty.md"), str(root / "config.example.json")], cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["config"]["llm_mode"], "codex")
            self.assertFalse(payload["config"]["allow_paid_api"])

    def test_unshipped_flag_injected_copy_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = write_doc(
                root,
                "badflag.md",
                "```bash paper-curation-command\n"
                + "python pipeline/run_full.py --topic qa_fixture --paid" + "-provider fake --dry-run\n"
                + "```\n",
            )
            result = run_validator([str(doc)])
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["rows"][0]["result"], "fail")

    def test_secret_scan_rejects_synthetic_key_canary(self) -> None:
        scanner = REPO_ROOT / "scripts" / "scan-secrets.py"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = write_doc(
                root,
                "canary.md",
                "```bash paper-curation-command\n"
                + "python pipeline/run_full.py --topic qa --dry-run\n"
                + "```\n"
                + "synthetic canary: sk-" + "ant-" + "A" * 56 + "\n",
            )
            result = subprocess.run(
                [PY, str(scanner), "--working-tree", "--object-range", "fc49f2672dcbb4779fa36b31ea5eceb44c090503..HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_tagged_block_has_exactly_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc = write_doc(
                root,
                "multi.md",
                "```bash paper-curation-command\n"
                "python pipeline/run_full.py --topic qa --dry-run\n"
                "```\n"
                "```bash paper-curation-command\n"
                "python pipeline/doctor.py --format json\n"
                "```\n"
                "```bash paper-curation-command\n"
                "python pipeline/run_update_force.py --topic qa --dry-run\n"
                "```\n",
            )
            result = run_validator([str(doc)])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["rows"]), 3)


class ShippedDocsTests(unittest.TestCase):
    """The real repository docs must pass once updated (Todo 22 green gate)."""

    def test_shipped_docs_pass_validation(self) -> None:
        paths = [
            "README.md",
            "README.en.md",
            "docs/setup-guide.md",
            "AGENTS.md",
            "config.example.json",
        ]
        result = run_validator(paths)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["result"], "PASS")
        self.assertGreater(len(payload["rows"]), 0)

    def test_shipped_skill_invokes_only_shipped_flags(self) -> None:
        skill = REPO_ROOT / "SKILL.md"
        if not skill.exists():
            self.skipTest("SKILL.md not present")
        result = run_validator([str(skill)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
