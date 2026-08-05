"""Doctor exposes a verify-only Codex canary surface."""

from __future__ import annotations

import subprocess
import sys
import unittest
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / "pipeline/doctor.py"


class DoctorCodexTests(unittest.TestCase):
    def test_doctor_declares_verify_only_codex_canary(self) -> None:
        # Given: the real Doctor CLI.
        # When: its help surface is rendered.
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        result = subprocess.run([sys.executable, str(DOCTOR), "--help"], cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", check=False)

        # Then: the narrow canary mode is available without an acceptance flag.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--codex-canary", result.stdout)
        self.assertNotIn("accept-current-signed-binary", result.stdout)


if __name__ == "__main__":
    unittest.main()
