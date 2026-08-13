"""Regression coverage for sparse cross-index provenance."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pipeline.build_cross_index as cross
from pipeline.sparse_index import (
    DurableIO,
    DurabilityError,
    SPARSE_SCHEMA,
    SparseIndexError,
    build_cross_sparse_index,
    build_sparse_index,
    restore_transaction,
)


class CrossIndexFingerprintTests(unittest.TestCase):
    def _source(self, root: Path, topic: str, text: str) -> None:
        papers = root / "papers"
        papers.mkdir(parents=True, exist_ok=True)
        index_path = papers / "_papers_index.json"
        rows = (
            cast(
                list[dict[str, object]],
                cast(
                    object,
                    json.loads(index_path.read_text(encoding="utf-8")),
                ),
            )
            if index_path.exists()
            else []
        )
        slug = f"{topic}-paper"
        rows = [row for row in rows if row["slug"] != slug]
        rows.append({"slug": slug, "title": topic, "topics": [topic]})
        _ = index_path.write_text(json.dumps(rows), encoding="utf-8")
        review = papers / slug
        review.mkdir(exist_ok=True)
        _ = (review / "review.md").write_text(
            f"# {topic}\n\n## Essence\n{text}\n",
            encoding="utf-8",
        )
        (root / topic).mkdir(exist_ok=True)
        _ = build_sparse_index(topic, root)

    def test_merge_records_sources_and_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._source(root, "alpha", "agent graph")
            self._source(root, "beta", "model graph")
            with patch.object(cross, "DOCS_DIR", root):
                first, sidecar, sources = cross.merge_indexes(["alpha", "beta"])
                self.assertEqual(first["schema"], SPARSE_SCHEMA)
                self.assertEqual(first["source_file_count"], 2)
                self.assertEqual(sidecar, b"")
                self.assertIn("indexes", sources)
                first_fingerprint = first["source_fingerprint"]
                self._source(root, "alpha", "agent graph changed")
                second, _, _ = cross.merge_indexes(["alpha", "beta"])
            self.assertNotEqual(
                first_fingerprint,
                second["source_fingerprint"],
            )
            self.assertEqual(list((root / "_cross").glob("*.bin")), [])

    def test_merge_rejects_non_sparse_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            topic = root / "alpha"
            topic.mkdir()
            _ = (topic / cross.SEARCH_INDEX).write_text(
                json.dumps({"papers": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(SparseIndexError):
                _ = cross.build_cross_index(["alpha"], docs_dir=root)

    def test_cross_journal_restores_prior_sparse_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._source(root, "alpha", "agent graph")
            first = build_cross_sparse_index(
                ["alpha"],
                root,
                run_id="cross-first",
            )
            first_hash = first.active_path.read_bytes()
            self._source(root, "alpha", "agent graph revised")
            second = build_cross_sparse_index(
                ["alpha"],
                root,
                run_id="cross-second",
            )
            manifest = cast(Path, second.manifest_path)
            restored = restore_transaction(manifest, root)
            self.assertEqual(restored.phase, "restored")
            self.assertEqual(first_hash, restored.active_path.read_bytes())

    def test_cross_preflight_failure_creates_no_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._source(root, "alpha", "agent graph")

            class Refuse:
                def preflight(self, _root: Path, _paths: list[Path]) -> None:
                    raise DurabilityError("preflight-refused")

                def durable_write(self, _path: Path, _data: bytes) -> None:
                    raise AssertionError("write after refusal")

                def move(self, _source: Path, _target: Path) -> None:
                    raise AssertionError("move after refusal")

                def sync_file(self, _path: Path) -> None:
                    raise AssertionError("sync after refusal")

            with self.assertRaises(DurabilityError):
                _ = build_cross_sparse_index(
                    ["alpha"],
                    root,
                    durability=cast(
                        DurableIO,
                        cast(object, Refuse()),
                    ),
                )
            self.assertFalse((root / "_cross").exists())

    def test_cross_rejects_corrupt_child_and_self_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._source(root, "alpha", "agent graph")
            active = root / "alpha" / cross.SEARCH_INDEX
            value = cast(
                dict[str, object],
                cast(object, json.loads(active.read_text(encoding="utf-8"))),
            )
            documents = cast(list[dict[str, object]], value["documents"])
            documents[0]["length"] = 99
            _ = active.write_text(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(SparseIndexError):
                _ = build_cross_sparse_index(["alpha"], root)
            self.assertFalse((root / "_cross").exists())

            self._source(root, "alpha", "agent graph")
            _ = build_cross_sparse_index(["alpha"], root)
            with self.assertRaisesRegex(
                SparseIndexError,
                "cross-self-source-refused",
            ):
                _ = build_cross_sparse_index(["_cross"], root)

    def test_cross_cli_refreshes_installed_evaluation_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "docs"
            self._source(root, "alpha", "agent graph")
            with patch.object(
                cross,
                "refresh_evaluation_snapshot",
                return_value=0,
            ) as refresh:
                exit_code = cross.main(
                    [
                        "--docs-dir",
                        str(root),
                        "alpha",
                    ]
                )
            self.assertEqual(exit_code, 0)
            refresh.assert_called_once_with(
                [
                    "--project-root",
                    str(root.resolve().parent),
                    "--if-installed",
                ]
            )


if __name__ == "__main__":
    _ = unittest.main()
