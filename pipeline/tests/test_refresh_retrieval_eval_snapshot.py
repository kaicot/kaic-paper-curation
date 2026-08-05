"""Coverage for the TCC-safe retrieval evaluation snapshot."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline.refresh_retrieval_eval_snapshot import (
    EVAL_FILES,
    RUNTIME_FILES,
    refresh_snapshot,
)
from pipeline.sparse_index import build_sparse_index


class RetrievalEvalSnapshotTests(unittest.TestCase):
    def test_schedulers_use_the_lexical_baseline(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        for relative in (
            "scripts/run-weekly-retrieval-eval.sh",
            "scripts/install-retrieval-eval-launchd.sh",
        ):
            source = (repository / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn("--baseline", source)
                self.assertIn("--max-regression", source)
                self.assertNotIn("--vectors", source)

    def test_refresh_copies_runtime_eval_and_collection_indexes_atomically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            output = Path(td) / "snapshot"
            _ = (root / "pipeline" / "eval").mkdir(parents=True)
            _ = (root / "docs" / "demo").mkdir(parents=True)
            papers = root / "docs" / "papers"
            paper = papers / "001_Alpha"
            _ = paper.mkdir(parents=True)
            _ = (papers / "_papers_index.json").write_text(
                json.dumps(
                    [
                        {
                            "slug": "001_Alpha",
                            "title": "Alpha",
                            "topics": ["demo"],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            _ = (paper / "review.md").write_text(
                "# Alpha\n\n## Essence\nalpha\n",
                encoding="utf-8",
            )
            _ = build_sparse_index("demo", root / "docs")
            for name in RUNTIME_FILES:
                _ = shutil.copy2(
                    Path(__file__).resolve().parents[1] / name,
                    root / "pipeline" / name,
                )
            query = {
                "collection": "demo",
                "id": "q1",
                "query": "alpha",
                "relevant_slugs": ["001_Alpha"],
            }
            for name in EVAL_FILES:
                content = json.dumps(query) + "\n" if name == "retrieval_queries.jsonl" else "{}\n"
                _ = (root / "pipeline" / "eval" / name).write_text(content, encoding="utf-8")

            _ = refresh_snapshot(root, output)
            self.assertEqual(
                (
                    output / "pipeline" / "evaluate_retrieval.py"
                ).read_bytes(),
                (
                    Path(__file__).resolve().parents[1]
                    / "evaluate_retrieval.py"
                ).read_bytes(),
            )
            self.assertFalse(
                (output / "docs" / "demo" / "_search_index_emb.bin").exists()
            )
            self.assertTrue(
                (output / "pipeline" / "sparse_index.py").is_file()
            )
            self.assertTrue(
                (output / "docs" / "papers" / "_papers_index.json").is_file()
            )
            self.assertTrue(
                (
                    output
                    / "docs"
                    / "papers"
                    / "001_Alpha"
                    / "review.md"
                ).is_file()
            )
            evaluation = subprocess.run(
                [
                    sys.executable,
                    str(output / "pipeline" / "evaluate_retrieval.py"),
                    "--queries",
                    str(
                        output
                        / "pipeline"
                        / "eval"
                        / "retrieval_queries.jsonl"
                    ),
                    "--all",
                    "--docs-dir",
                    str(output / "docs"),
                    "--output",
                    str(output / "report.json"),
                    "--min-recall-at-5",
                    "0",
                    "--strict",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(
                evaluation.returncode,
                0,
                evaluation.stdout + evaluation.stderr,
            )

            _ = (output / "stale").write_text("old", encoding="utf-8")
            _ = refresh_snapshot(root, output)
            self.assertFalse((output / "stale").exists())
            self.assertFalse(output.with_name(".snapshot.previous").exists())


if __name__ == "__main__":
    _ = unittest.main()
