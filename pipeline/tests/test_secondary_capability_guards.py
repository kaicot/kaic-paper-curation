"""Acceptance tests for the final secondary-capability zero boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, cast

from pipeline.secondary_capability_guard import (
    SecondaryCapabilityUnavailable,
    denial_payload,
)


ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = ROOT / "pipeline/tools/probe_secondary_entrypoints.py"
SPEC = importlib.util.spec_from_file_location("_secondary_probe_test", PROBE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("secondary probe module unavailable")
PROBE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROBE
SPEC.loader.exec_module(PROBE)
run_probe = cast(
    Callable[[Path, set[str], tuple[str, ...]], dict[str, object]],
    getattr(PROBE, "run_probe"),
)
ProbeError = cast(type[Exception], getattr(PROBE, "ProbeError"))


class SecondaryCapabilityGuardTests(unittest.TestCase):
    def test_safe_modes_are_unavailable_and_other_modes_denied(self) -> None:
        for mode in ("codex", "off"):
            with self.subTest(mode=mode):
                value = denial_payload("fixture", mode)
                self.assertEqual(value["status"], "unavailable")
                self.assertEqual(value["mode"], mode)
        with self.assertRaises(SecondaryCapabilityUnavailable):
            _ = denial_payload("fixture", "api")

    def test_legacy_paid_flags_and_keys_never_enable_capability(self) -> None:
        target = ROOT / "pipeline/generate_audio.py"
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().endswith("_API_KEY")
        }
        for key in (
            "ANTH" + "ROPIC_API_KEY",
            "OPEN" + "AI_API_KEY",
            "GOO" + "GLE_API_KEY",
            "GEM" + "INI_API_KEY",
        ):
            environment[key] = "poison"
        with tempfile.TemporaryDirectory(prefix="legacy-denial-") as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(target),
                    "--llm-mode",
                    "codex",
                    "--api-provider",
                    "paid",
                    "--acknowledge-paid-api-cost",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
            self.assertEqual(result.returncode, 2)
            payload = cast(
                dict[str, object],
                cast(object, json.loads(result.stdout)),
            )
            self.assertEqual(payload["status"], "unavailable")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_manifest_probe_covers_every_selected_path(self) -> None:
        manifest_path = ROOT / "pipeline/provider-entrypoints.json"
        manifest = cast(
            dict[str, object],
            cast(
                object,
                json.loads(manifest_path.read_text(encoding="utf-8")),
            ),
        )
        manifest_rows = cast(list[dict[str, object]], manifest["entrypoints"])
        selected = [
            row
            for row in manifest_rows
            if row["disposition"] in {"quarantine", "default-denied"}
        ]
        report = run_probe(
            manifest_path,
            {"quarantine", "default-denied"},
            ("codex", "off"),
        )
        rows = cast(list[dict[str, object]], report["rows"])
        self.assertEqual(len(rows), len(selected))
        self.assertEqual(
            [row["path"] for row in rows],
            sorted(cast(str, row["path"]) for row in selected),
        )
        for row in rows:
            modes = cast(list[dict[str, object]], row["modes"])
            self.assertEqual(
                [mode["mode"] for mode in modes],
                ["codex", "off"],
            )
            self.assertTrue(
                all(
                    mode["status"] in {"unavailable", "migrated-local"}
                    for mode in modes
                )
            )

    def test_forbidden_manifest_disposition_fails_without_output(self) -> None:
        source = cast(
            dict[str, object],
            cast(
                object,
                json.loads(
                    (ROOT / "pipeline/provider-entrypoints.json").read_text(
                        encoding="utf-8"
                    )
                ),
            ),
        )
        source_rows = cast(list[dict[str, object]], source["entrypoints"])
        source_rows[0]["disposition"] = "callable"
        with tempfile.TemporaryDirectory(prefix="capability-negative-") as directory:
            manifest = Path(directory) / "manifest.json"
            _ = manifest.write_text(
                json.dumps(source, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaises(ProbeError):
                _ = run_probe(
                    manifest,
                    {"quarantine", "default-denied"},
                    ("codex", "off"),
                )
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_default_static_pages_contain_no_retired_controls(self) -> None:
        topic_source = (
            ROOT / "pipeline/build_topic_index.py"
        ).read_text(encoding="utf-8").lower()
        review_source = (
            ROOT / "pipeline/review_to_html.py"
        ).read_text(encoding="utf-8").lower()
        for source in (topic_source, review_source):
            self.assertNotIn("api/embed", source)
            self.assertNotIn("api/audio-email", source)


if __name__ == "__main__":
    _ = unittest.main()
