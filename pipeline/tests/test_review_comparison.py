"""Comparison is isolated, resumable, and invalidated by its full source."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.tools import compare_review_models as comparison
from pipeline.tests.test_codex_review import _payload


class FakeGateway:
    contract = {"epoch": 2}
    policy = {"paid_api": False}
    last_run_metrics = {"output_tokens": 100}
    last_run_provenance = {"cli_version": "qualified-test", "binary_sha256": "a" * 64}

    def __init__(self):
        self.calls = 0

    def generate_json(self, role, prompt, schema):
        self.calls += 1
        return _payload()


class ReviewComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / ".omo/comparison"
        self.papers = self.root / "docs/papers"
        self.slugs = ["one", "two", "three"]
        for slug in self.slugs:
            paper = self.papers / slug
            paper.mkdir(parents=True)
            (paper / "text.md").write_text("Methods\n원문 방법입니다.\nResults\n결과입니다.", encoding="utf-8")
            (paper / "review.md").write_text("original", encoding="utf-8")
        (self.papers / "_papers_index.json").write_text("[]", encoding="utf-8")
        self.gateway = FakeGateway()

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, execute=True, slugs=None):
        args = ["compare", "--slugs", slugs or ",".join(self.slugs), "--output-dir", str(self.output)]
        if execute:
            args.append("--execute")
        with patch.object(comparison, "ROOT", self.root), patch("sys.argv", args), patch.object(
            comparison, "prepare_gateway", return_value=self.gateway
        ) as prepare:
            result = comparison.main()
            return result, prepare.call_count

    def test_plan_does_not_create_output_or_gateway(self):
        self.assertEqual(self.run_tool(False), (0, 0))
        self.assertFalse(self.output.exists())
        with self.assertRaises(SystemExit):
            self.run_tool(False, "one,one,two")
        with self.assertRaises(SystemExit):
            self.run_tool(False, "one,two,../outside")

    def test_resume_rebuilds_report_and_source_changes_invalidate(self):
        self.assertEqual(self.run_tool()[0], 0)
        self.assertEqual(self.gateway.calls, 6)
        (self.output / "report.json").unlink()
        self.run_tool()
        self.assertEqual(self.gateway.calls, 6)
        report = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["completed"], 6)
        self.assertEqual(report["results"][0]["runtime_provenance"], self.gateway.last_run_provenance)
        # Deliberately hold selected prompt constant: even omitted source changes
        # must invalidate the comparison record.
        with patch.object(comparison, "build_review_source", return_value=("same selection", {})):
            self.run_tool()
            calls = self.gateway.calls
            (self.papers / "one/text.md").write_text("changed outside selected excerpt", encoding="utf-8")
            self.run_tool()
            self.assertEqual(self.gateway.calls, calls + 2)
        for slug in self.slugs:
            self.assertEqual((self.papers / slug / "review.md").read_text(), "original")

    def test_corrupt_cached_row_is_regenerated(self):
        self.run_tool()
        (self.output / "one/gpt-6-astra.json").write_text("{broken", encoding="utf-8")
        self.run_tool()
        self.assertEqual(self.gateway.calls, 7)


if __name__ == "__main__":
    unittest.main()
