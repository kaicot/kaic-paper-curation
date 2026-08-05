from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import cast
from unittest.mock import patch

from pipeline import run_full, run_update_force
from pipeline.release_dry_run import DEFAULT_VALIDATOR_STAGES
from pipeline.tools import check_mutating_entrypoints


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable).resolve()
MANIFEST = ROOT / "pipeline" / "mutating-entrypoints.json"
CHECKER = ROOT / "pipeline" / "tools" / "check_mutating_entrypoints.py"


def _tree_digest(root: Path) -> str:
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rows.append(
                (
                    path.relative_to(root).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode()
    ).hexdigest()


class ReleaseDryRunTests(unittest.TestCase):
    def test_run_full_dry_run_is_one_json_document_and_strictly_inert(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            sentinel = workspace / "operator.txt"
            _ = sentinel.write_text("preserve", encoding="utf-8")
            before = _tree_digest(workspace)
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                patch.object(
                    run_full,
                    "run",
                    side_effect=AssertionError("child process"),
                ),
            ):
                result = run_full.main(
                    [
                        "--topic",
                        "qa_fixture",
                        "--dry-run",
                        "--docs-dir",
                        str(workspace / "docs"),
                    ]
                )
            payload = cast(
                dict[str, object],
                json.loads(output.getvalue()),
            )
            self.assertEqual(result, 0)
            self.assertEqual(payload["schema"], "dry-run-plan-v1")
            self.assertEqual(payload["validators"], list(DEFAULT_VALIDATOR_STAGES))
            defaults = cast(dict[str, object], payload["defaults"])
            self.assertEqual(defaults["concurrency"], 1)
            self.assertFalse(defaults["deploy"])
            self.assertEqual(payload["merkle_before"], payload["merkle_after"])
            self.assertEqual(payload["read_set"], [])
            self.assertEqual(payload["writes"], [])
            self.assertEqual(payload["egress"], [])
            self.assertEqual(payload["children"], [])
            self.assertEqual(payload["deploy_attempts"], [])
            self.assertEqual(payload["forbidden_counters"], {
                "auth": 0,
                "children": 0,
                "credentials": 0,
                "deploy": 0,
                "egress": 0,
                "git": 0,
                "hashes": 0,
                "writes": 0,
            })
            self.assertEqual(_tree_digest(workspace), before)

    def test_run_update_force_dry_run_precedes_every_impure_boundary(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(
                run_update_force,
                "_initialize_runtime",
                side_effect=AssertionError("runtime"),
            ),
            patch.object(
                run_update_force,
                "fetch_zotero_items",
                side_effect=AssertionError("network"),
            ),
            patch.object(
                run_update_force,
                "save_checkpoint",
                side_effect=AssertionError("write"),
            ),
        ):
            result = run_update_force.main(
                ["--topic", "qa_fixture", "--dry-run"]
            )
        payload = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(result, 0)
        self.assertEqual(payload["schema"], "dry-run-plan-v1")
        self.assertEqual(payload["entrypoint"], "pipeline/run_update_force.py")

    def test_run_update_force_off_precedes_config_and_generation(self) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(
                run_update_force,
                "load_config",
                side_effect=AssertionError("config body read"),
            ),
            patch.object(
                run_update_force,
                "_initialize_runtime",
                side_effect=AssertionError("credentials"),
            ),
            patch.object(
                run_update_force,
                "fetch_zotero_items",
                side_effect=AssertionError("network"),
            ),
        ):
            result = run_update_force.main(
                ["--topic", "qa_fixture", "--llm-mode", "off"]
            )
        payload = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(result, 3)
        self.assertEqual(payload["schema"], "run-result-v1")
        self.assertEqual(payload["status"], "policy_denied")

    def test_manifest_checker_and_unclassified_negative_fixture(self) -> None:
        result = subprocess.run(
            [
                str(PYTHON),
                str(CHECKER),
                "--manifest",
                str(MANIFEST),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(payload["result"], "PASS")
        self.assertEqual(payload["unclassified"], 0)
        self.assertEqual(payload["forbidden_counters"], {
            "auth": 0,
            "children": 0,
            "credentials": 0,
            "deploy": 0,
            "egress": 0,
            "git": 0,
            "hashes": 0,
            "writes": 0,
        })

        fixture = ROOT / "pipeline" / f"run_task21_{uuid.uuid4().hex}.py"
        marker = ROOT / "must-not-run"
        try:
            _ = fixture.write_text(
                "\n".join(
                    [
                        "import argparse",
                        "from pathlib import Path",
                        "p = argparse.ArgumentParser()",
                        "if __name__ == '__main__':",
                        "    Path('must-not-run').write_text('bad')",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            failed = subprocess.run(
                [
                    str(PYTHON),
                    str(CHECKER),
                    "--manifest",
                    str(MANIFEST),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertIn("unclassified-mutator", failed.stderr)
            self.assertFalse(marker.exists())
        finally:
            _ = fixture.unlink(missing_ok=True)
            _ = marker.unlink(missing_ok=True)

    def test_probe_rejects_undeclared_body_and_credential_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            body = root / "paper.md"
            _ = body.write_text("private body", encoding="utf-8")
            for name, operation in (
                (
                    "body",
                    "\n".join(
                        [
                            "from pathlib import Path",
                            "_ = Path('paper.md').read_text(encoding='utf-8')",
                            "",
                        ]
                    ),
                ),
                (
                    "credential",
                    "\n".join(
                        [
                            "import os",
                            "_ = os.environ.get('EXAMPLE_' + 'API_KEY')",
                            "",
                        ]
                    ),
                ),
            ):
                with self.subTest(name=name):
                    script = root / f"{name}.py"
                    _ = script.write_text(operation, encoding="utf-8")
                    row: dict[str, object] = {
                        "dry_run_argv": [
                            "{python}",
                            script.name,
                        ],
                        "path": script.name,
                    }
                    with self.assertRaises(
                        check_mutating_entrypoints.InventoryError
                    ) as caught:
                        check_mutating_entrypoints.probe_rows([row], root)
                    self.assertIn("dry-run-forbidden", str(caught.exception))


if __name__ == "__main__":
    _ = unittest.main()
