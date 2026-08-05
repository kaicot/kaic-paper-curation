"""Deterministic BM25-only retrieval evaluation coverage."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from pipeline import evaluate_retrieval as evaluator
from pipeline.sparse_index import build_sparse_index


class RetrievalEvaluatorTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    root: Path = Path()
    docs: Path = Path()
    query_path: Path = Path()

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="bm25-eval-")
        self.root = Path(self.temporary.name)
        self.docs = self.root / "docs"
        papers = self.docs / "papers"
        topic = self.docs / "demo"
        papers.mkdir(parents=True)
        topic.mkdir()
        _ = (papers / "_papers_index.json").write_text(
            json.dumps(
                [
                    {
                        "slug": "001_Alpha",
                        "title": "Alpha",
                        "topics": ["demo"],
                    },
                    {
                        "slug": "002_Beta",
                        "title": "Beta",
                        "topics": ["demo"],
                    },
                ]
            ),
            encoding="utf-8",
        )
        for slug, text in (
            ("001_Alpha", "alpha agent"),
            ("002_Beta", "beta model"),
        ):
            directory = papers / slug
            directory.mkdir()
            _ = (directory / "review.md").write_text(
                f"# {slug}\n\n## Essence\n{text}\n",
                encoding="utf-8",
            )
        _ = build_sparse_index("demo", self.docs)
        self.query_path = self.root / "queries.jsonl"
        _ = self.query_path.write_text(
            json.dumps(
                {
                    "collection": "demo",
                    "id": "q1",
                    "query": "alpha",
                    "relevant_slugs": ["001_Alpha"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_query_set_hash_normalizes_line_endings(self) -> None:
        rows, lf_hash = evaluator.load_query_set(self.query_path)
        crlf = self.root / "queries-crlf.jsonl"
        normalized = self.query_path.read_bytes().replace(b"\r\n", b"\n")
        _ = crlf.write_bytes(normalized.replace(b"\n", b"\r\n"))
        other_rows, crlf_hash = evaluator.load_query_set(crlf)
        self.assertEqual(rows, other_rows)
        self.assertEqual(lf_hash, crlf_hash)
        self.assertEqual(
            lf_hash,
            hashlib.sha256(normalized).hexdigest(),
        )

    def test_evaluate_rows_calls_bm25_without_vectors(self) -> None:
        rows, _ = evaluator.load_query_set(self.query_path)
        response: dict[str, object] = {
            "status": "ok",
            "results": [{"slug": "001_Alpha"}],
        }
        calls: list[tuple[str, str, dict[str, object]]] = []

        def fake_query(
            topic: str,
            query: str,
            **options: object,
        ) -> dict[str, object]:
            calls.append((topic, query, options))
            return response

        with patch.object(
            evaluator,
            "query_search_index",
            side_effect=fake_query,
        ):
            evaluations = evaluator.evaluate_rows(rows, docs_dir=self.docs)
        self.assertEqual(evaluations[0]["ranks"]["001_Alpha"], 1)
        topic, query, keywords = calls[0]
        self.assertEqual((topic, query), ("demo", "alpha"))
        self.assertNotIn("mode", keywords)
        self.assertNotIn("query_vector", keywords)
        self.assertEqual(keywords["top_k"], evaluator.MAX_K)

    def test_non_ok_query_status_aborts_evaluation(self) -> None:
        rows, _ = evaluator.load_query_set(self.query_path)
        with patch.object(
            evaluator,
            "query_search_index",
            return_value={
                "status": "stale-index",
                "code": "source-fingerprint-mismatch",
                "results": [],
            },
        ):
            with self.assertRaisesRegex(
                evaluator.EvaluationError,
                "stale-index",
            ):
                _ = evaluator.evaluate_rows(rows, docs_dir=self.docs)

    def test_metrics_and_failure_rows_preserve_ranking_contract(self) -> None:
        evaluations = [
            {
                "collection": "demo",
                "id": "q1",
                "query": "alpha",
                "ranks": {"001_Alpha": 1},
                "relevant_slugs": ["001_Alpha"],
                "top_slugs": ["001_Alpha"],
            },
            {
                "collection": "demo",
                "id": "q2",
                "query": "missing",
                "ranks": {"002_Beta": None},
                "relevant_slugs": ["002_Beta"],
                "top_slugs": [],
            },
        ]
        metrics = evaluator.compute_metrics(evaluations)
        self.assertEqual(metrics["aggregate"]["recall_at_5"], 0.5)
        report = evaluator.build_report(
            evaluations,
            metrics,
            query_set_sha256="a" * 64,
        )
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["retrieval_mode"], "bm25")
        self.assertEqual(
            len(cast(list[dict[str, object]], report["failures"])),
            1,
        )
        self.assertNotIn("vector_manifest_sha256", report)

    def test_baseline_identity_requires_schema_mode_and_query_hash(self) -> None:
        report = {
            "schema_version": 2,
            "query_set_sha256": "a" * 64,
            "retrieval_mode": "bm25",
        }
        baseline = dict(report)
        evaluator.validate_baseline_identity(baseline, report)
        for field, value in (
            ("schema_version", 1),
            ("query_set_sha256", "b" * 64),
            ("retrieval_mode", "hybrid"),
        ):
            changed = dict(baseline)
            changed[field] = value
            with self.subTest(field=field):
                with self.assertRaises(evaluator.EvaluationError):
                    evaluator.validate_baseline_identity(changed, report)

    def test_tracked_baseline_is_lexical_and_matches_query_set(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        rows, query_hash = evaluator.load_query_set(
            repository / "pipeline" / "eval" / "retrieval_queries.jsonl"
        )
        baseline = evaluator.load_baseline(
            repository / "pipeline" / "eval" / "retrieval_baseline.json"
        )
        self.assertEqual(baseline["schema_version"], 2)
        self.assertEqual(baseline["retrieval_mode"], "bm25")
        self.assertEqual(baseline["query_set_sha256"], query_hash)
        self.assertEqual(
            cast(dict[str, object], baseline["aggregate"])["query_count"],
            len(rows),
        )

    def test_real_cli_records_and_reuses_lexical_baseline(self) -> None:
        output = self.root / "report.json"
        baseline = self.root / "baseline.json"
        common = [
            "--queries",
            str(self.query_path),
            "--all",
            "--docs-dir",
            str(self.docs),
            "--output",
            str(output),
            "--baseline",
            str(baseline),
            "--min-recall-at-5",
            "0",
        ]
        self.assertEqual(
            evaluator.main([*common, "--record-baseline"]),
            0,
        )
        first_baseline = baseline.read_bytes()
        self.assertEqual(evaluator.main([*common, "--strict"]), 0)
        self.assertEqual(baseline.read_bytes(), first_baseline)
        report = cast(
            dict[str, object],
            cast(object, json.loads(output.read_text(encoding="utf-8"))),
        )
        self.assertEqual(report["retrieval_mode"], "bm25")
        self.assertNotIn("vector_manifest_sha256", report)

    def test_missing_relevant_slug_fails_before_report_write(self) -> None:
        _ = self.query_path.write_text(
            json.dumps(
                {
                    "collection": "demo",
                    "id": "q1",
                    "query": "alpha",
                    "relevant_slugs": ["999_Missing"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        output = self.root / "report.json"
        self.assertEqual(
            evaluator.main(
                [
                    "--queries",
                    str(self.query_path),
                    "--all",
                    "--docs-dir",
                    str(self.docs),
                    "--output",
                    str(output),
                ]
            ),
            2,
        )
        self.assertFalse(output.exists())

    def test_stale_index_preserves_prior_report(self) -> None:
        output = self.root / "report.json"
        _ = output.write_text('{"preserve":true}\n', encoding="utf-8")
        before = output.read_bytes()
        review = self.docs / "papers" / "001_Alpha" / "review.md"
        _ = review.write_text(
            "# Alpha\n\n## Essence\nchanged source\n",
            encoding="utf-8",
        )
        exit_code = evaluator.main(
            [
                "--queries",
                str(self.query_path),
                "--all",
                "--docs-dir",
                str(self.docs),
                "--output",
                str(output),
            ]
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(output.read_bytes(), before)

    def test_invalid_threshold_and_topic_baseline_are_rejected(self) -> None:
        output = self.root / "report.json"
        base = [
            "--queries",
            str(self.query_path),
            "--topic",
            "demo",
            "--docs-dir",
            str(self.docs),
            "--output",
            str(output),
        ]
        self.assertEqual(
            evaluator.main([*base, "--max-regression", "nan"]),
            2,
        )
        self.assertEqual(
            evaluator.main(
                [
                    *base,
                    "--baseline",
                    str(self.root / "baseline.json"),
                    "--record-baseline",
                ]
            ),
            2,
        )


if __name__ == "__main__":
    _ = unittest.main()
