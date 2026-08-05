"""BM25-default sparse query contract and process-boundary coverage."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

from pipeline.query_search_index import (
    QUERY_SCHEMA,
    main,
    query_search_index,
)
from pipeline.sparse_index import (
    build_cross_sparse_index,
    build_sparse_index,
)


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable).resolve()


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(item.read_bytes())
    return digest.hexdigest()


def _write_topic(
    docs: Path,
    topic: str,
    rows: list[dict[str, object]],
    reviews: dict[str, str],
) -> None:
    papers = docs / "papers"
    papers.mkdir(parents=True, exist_ok=True)
    index_path = papers / "_papers_index.json"
    existing = (
        cast(
            list[dict[str, object]],
            cast(object, json.loads(index_path.read_text(encoding="utf-8"))),
        )
        if index_path.exists()
        else []
    )
    existing = [
        row
        for row in existing
        if str(row.get("slug", "")) not in reviews
    ]
    existing.extend(rows)
    _ = index_path.write_text(
        json.dumps(existing, ensure_ascii=False),
        encoding="utf-8",
    )
    for slug, review in reviews.items():
        directory = papers / slug
        directory.mkdir(exist_ok=True)
        _ = (directory / "review.md").write_text(review, encoding="utf-8")
    (docs / topic).mkdir(exist_ok=True)
    _ = build_sparse_index(topic, docs)


class QuerySearchIndexTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    docs: Path = Path()

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="bm25-query-")
        self.docs = Path(self.temporary.name) / "docs"
        _write_topic(
            self.docs,
            "demo",
            [
                {"slug": "002_Beta", "title": "Beta", "topics": ["demo"]},
                {"slug": "001_Alpha", "title": "Alpha", "topics": ["demo"]},
            ],
            {
                "001_Alpha": "# Alpha\n\n## Essence\nalpha agent 한국어\n",
                "002_Beta": "# Beta\n\n## Essence\nbeta model\n",
            },
        )

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_bm25_returns_known_paper_and_zero_dense_score(self) -> None:
        result = query_search_index("demo", "alpha", docs_dir=self.docs)
        self.assertEqual(result["schema"], QUERY_SCHEMA)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "bm25")
        rows = cast(list[dict[str, object]], result["results"])
        self.assertEqual(rows[0]["slug"], "001_Alpha")
        self.assertGreater(cast(float, rows[0]["score"]), 0)
        self.assertEqual(rows[0]["dense_score"], 0.0)
        self.assertEqual(rows[0]["matched_terms"], ["alpha"])

    def test_cli_process_is_read_only_and_never_uses_network_or_keys(self) -> None:
        before = _tree_digest(self.docs)
        code = (
            "import socket,sys;"
            f"sys.path.insert(0,{str(ROOT)!r});"
            "socket.create_connection=lambda *a,**k:"
            "(_ for _ in ()).throw(RuntimeError('egress'));"
            "socket.socket.connect=lambda *a,**k:"
            "(_ for _ in ()).throw(RuntimeError('egress'));"
            "from pipeline.query_search_index import main;"
            "raise SystemExit(main())"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "ANTH" + "ROPIC_API_KEY": "poison",
                "OPEN" + "AI_API_KEY": "poison",
                "GOO" + "GLE_API_KEY": "poison",
                "PYTHONUTF8": "1",
            }
        )
        completed = subprocess.run(
            [
                str(PYTHON),
                "-c",
                code,
                "--topic",
                "demo",
                "--query",
                "alpha",
                "--docs-dir",
                str(self.docs),
                "--json",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = cast(
            dict[str, object],
            cast(object, json.loads(completed.stdout)),
        )
        rows = cast(list[dict[str, object]], payload["results"])
        self.assertEqual(rows[0]["slug"], "001_Alpha")
        self.assertEqual(rows[0]["dense_score"], 0.0)
        self.assertEqual(_tree_digest(self.docs), before)

    def test_legacy_stale_and_empty_indexes_are_typed_non_success(self) -> None:
        active = self.docs / "demo" / "_search_index.json"
        _ = active.write_text(
            json.dumps(
                {
                    "chunks": [],
                    "dim": 768,
                    "model": "retired",
                    "papers": {},
                    "quant": "int8-l2norm",
                }
            ),
            encoding="utf-8",
        )
        legacy = query_search_index("demo", "alpha", docs_dir=self.docs)
        self.assertEqual(legacy["status"], "unsupported-index")
        self.assertIn("build_search_index.py", str(legacy["rebuild_command"]))

        _ = build_sparse_index("demo", self.docs)
        review = self.docs / "papers" / "001_Alpha" / "review.md"
        _ = review.write_text(
            "# Alpha\n\n## Essence\nchanged source\n",
            encoding="utf-8",
        )
        stale = query_search_index("demo", "alpha", docs_dir=self.docs)
        self.assertEqual(stale["status"], "stale-index")

        empty_docs = Path(self.temporary.name) / "empty-docs"
        _write_topic(empty_docs, "empty", [], {})
        empty = query_search_index("empty", "alpha", docs_dir=empty_docs)
        self.assertEqual(empty["status"], "empty-index")

    def test_dense_and_hybrid_are_denied_without_fallback(self) -> None:
        for mode in ("dense", "hybrid", "api"):
            with self.subTest(mode=mode):
                result = query_search_index(
                    "demo",
                    "alpha",
                    mode=mode,
                    docs_dir=self.docs,
                )
                self.assertEqual(result["status"], "unsupported-mode")
                self.assertEqual(result["results"], [])

    def test_query_terms_are_deduplicated_and_tokenless_input_is_denied(
        self,
    ) -> None:
        single = query_search_index(
            "demo",
            "agent",
            docs_dir=self.docs,
        )
        repeated = query_search_index(
            "demo",
            "agent agent agent",
            docs_dir=self.docs,
        )
        self.assertEqual(single["results"], repeated["results"])
        self.assertEqual(repeated["query_terms"], ["agent"])
        no_match = query_search_index(
            "demo",
            "unmatched",
            docs_dir=self.docs,
        )
        self.assertEqual(no_match["status"], "ok")
        self.assertEqual(no_match["results"], [])
        tokenless = query_search_index(
            "demo",
            "!!!",
            docs_dir=self.docs,
        )
        self.assertEqual(tokenless["status"], "invalid-query")
        self.assertEqual(tokenless["code"], "empty-query-terms")

    def test_cross_index_queries_and_detects_source_drift(self) -> None:
        _write_topic(
            self.docs,
            "other",
            [{"slug": "003_Gamma", "title": "Gamma", "topics": ["other"]}],
            {"003_Gamma": "# Gamma\n\n## Essence\ngamma catalyst\n"},
        )
        _ = build_cross_sparse_index(["demo", "other"], self.docs)
        result = query_search_index(None, "gamma", docs_dir=self.docs)
        self.assertEqual(result["topic"], "_cross")
        rows = cast(list[dict[str, object]], result["results"])
        self.assertEqual(rows[0]["slug"], "003_Gamma")
        source = self.docs / "other" / "_search_index.json"
        _ = source.write_bytes(source.read_bytes() + b"\n")
        stale = query_search_index(None, "gamma", docs_dir=self.docs)
        self.assertEqual(stale["status"], "stale-index")

    def test_invalid_postings_fail_closed_without_writes(self) -> None:
        active = self.docs / "demo" / "_search_index.json"
        value = cast(
            dict[str, object],
            cast(object, json.loads(active.read_text(encoding="utf-8"))),
        )
        postings = cast(dict[str, object], value["postings"])
        postings["alpha"] = [[99, 1]]
        _ = active.write_text(json.dumps(value), encoding="utf-8")
        before = _tree_digest(self.docs)
        result = query_search_index("demo", "alpha", docs_dir=self.docs)
        self.assertEqual(result["status"], "invalid-index")
        self.assertEqual(_tree_digest(self.docs), before)

    def test_nonfinite_numeric_json_returns_typed_failure(self) -> None:
        active = self.docs / "demo" / "_search_index.json"
        text = active.read_text(encoding="utf-8")
        text = text.replace(
            '"average_document_length":4.0',
            '"average_document_length":1e999',
        )
        _ = active.write_text(text, encoding="utf-8")
        completed = subprocess.run(
            [
                str(PYTHON),
                str(ROOT / "pipeline" / "query_search_index.py"),
                "--topic",
                "demo",
                "--query",
                "alpha",
                "--docs-dir",
                str(self.docs),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        payload = cast(
            dict[str, object],
            cast(object, json.loads(completed.stdout)),
        )
        self.assertEqual(payload["status"], "invalid-index")
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_returns_nonzero_for_legacy_index(self) -> None:
        active = self.docs / "demo" / "_search_index.json"
        _ = active.write_text('{"papers":{}}', encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "--topic",
                    "demo",
                    "--query",
                    "alpha",
                    "--docs-dir",
                    str(self.docs),
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertTrue(stdout.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
