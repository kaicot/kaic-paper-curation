"""Transactional acceptance coverage for the sparse-index-v2 builder."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from pipeline.build_search_index import main as index_main
from pipeline.sparse_index import (
    FAILPOINTS,
    MOVEFILE_REPLACE_EXISTING,
    MOVEFILE_WRITE_THROUGH,
    SPARSE_SCHEMA,
    BuildResult,
    DurableIO,
    DurabilityError,
    SparseIndexError,
    build_sparse_index,
    purge_transaction,
    recover_transaction,
    restore_transaction,
    tokenize,
)


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable).resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_source(docs: Path) -> None:
    papers = docs / "papers"
    papers.mkdir(parents=True)
    rows = [
        {
            "slug": "002_Beta",
            "title": "Beta discovery",
            "essence": "Graph agent chemistry",
            "abstract": "Agent graph catalyst",
            "topics": ["fixture"],
        },
        {
            "slug": "001_Alpha",
            "title": "Alpha discovery",
            "essence": "Graph model biology",
            "abstract": "Model graph protein",
            "classifications": {"fixture": {"primary_category": "Local"}},
        },
        {
            "slug": "999_Other",
            "title": "Excluded",
            "topics": ["other"],
        },
    ]
    _ = (papers / "_papers_index.json").write_text(
        json.dumps(rows, ensure_ascii=False),
        encoding="utf-8",
    )
    reviews = {
        "001_Alpha": "# Alpha\n\n## Essence\nGraph graph biology\n",
        "002_Beta": "# Beta\n\n## Essence\nGraph graph chemistry\n",
    }
    for slug, review in reviews.items():
        directory = papers / slug
        directory.mkdir()
        _ = (directory / "review.md").write_text(review, encoding="utf-8")
    (docs / "fixture").mkdir()


def _legacy_payload() -> dict[str, object]:
    return {
        "chunks": [],
        "dim": 768,
        "emb_file": "_search_index_emb.bin",
        "model": "retired-embedding-model",
        "papers": {"001_Alpha": {"title": "old"}},
        "quant": "int8-l2norm",
        "schema_version": 1,
    }


def _seed_legacy(topic: Path) -> dict[str, str]:
    active = topic / "_search_index.json"
    sidecar = topic / "_search_index_emb.bin"
    cache = topic / "_embedding_cache.json"
    sentinel = topic / "user-sentinel.txt"
    _ = active.write_text(
        json.dumps(_legacy_payload(), sort_keys=True),
        encoding="utf-8",
    )
    _ = sidecar.write_bytes(b"dense-legacy")
    _ = cache.write_text(
        json.dumps({"model": "retired", "vectors": {}}, sort_keys=True),
        encoding="utf-8",
    )
    _ = sentinel.write_text("preserve-me", encoding="utf-8")
    return {
        path.name: _sha256(path)
        for path in (active, sidecar, cache, sentinel)
    }


def _latest_manifest(topic: Path) -> Path:
    manifests = sorted(
        (
            topic
            / ".curation-quarantine"
            / "search-schema-v1"
        ).glob("*/manifest.json")
    )
    if not manifests:
        raise AssertionError("transaction manifest missing")
    return manifests[-1]


class SparseIndexBuildTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    docs: Path = Path()
    topic: Path = Path()

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sparse-v2-")
        self.docs = Path(self.temporary.name) / "docs"
        _write_source(self.docs)
        self.topic = self.docs / "fixture"

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_deterministic_two_paper_build_has_no_egress_or_cache(self) -> None:
        def network_denied(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("network attempted")

        source_hashes = {
            path.relative_to(self.docs).as_posix(): _sha256(path)
            for path in self.docs.rglob("*")
            if path.is_file()
        }
        poisoned = {
            "ANTH" + "ROPIC_API_KEY": "poison",
            "OPEN" + "AI_API_KEY": "poison",
            "GOO" + "GLE_API_KEY": "poison",
        }
        with (
            patch.object(socket, "create_connection", network_denied),
            patch.object(socket.socket, "connect", network_denied),
            patch.dict(os.environ, poisoned, clear=False),
        ):
            first = build_sparse_index("fixture", self.docs)
            first_bytes = first.active_path.read_bytes()
            reviews = sorted((self.docs / "papers").glob("*/review.md"))
            for review in reviews:
                os.utime(review, None)
            second = build_sparse_index("fixture", self.docs)

        self.assertIsInstance(first, BuildResult)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first_bytes, second.active_path.read_bytes())
        value = cast(
            dict[str, object],
            cast(object, json.loads(first_bytes)),
        )
        self.assertEqual(value["schema"], SPARSE_SCHEMA)
        self.assertEqual(value["schema_version"], 2)
        documents = cast(list[dict[str, object]], value["documents"])
        self.assertEqual(
            [document["slug"] for document in documents],
            ["001_Alpha", "002_Beta"],
        )
        postings = cast(dict[str, object], value["postings"])
        self.assertEqual(postings["graph"], [[0, 2], [1, 2]])
        self.assertEqual(
            cast(dict[str, object], value["bm25"]),
            {
                "b": 0.75,
                "idf": "ln(1+(N-df+0.5)/(df+0.5))",
                "k1": 1.5,
                "query_term_frequency": False,
                "tie_break": "document_id-ascending",
            },
        )
        self.assertNotIn("emb_file", value)
        self.assertEqual(
            list(self.topic.glob("*.bin")),
            [],
        )
        self.assertEqual(
            list(self.topic.rglob(".llm_cache")),
            [],
        )
        for relative, digest in source_hashes.items():
            self.assertEqual(_sha256(self.docs / relative), digest)
        self.assertEqual(
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            0x9,
        )

    def test_tokenizer_matches_ascii_and_hangul_query_contract(self) -> None:
        self.assertEqual(
            tokenize("GNN-2 한국어 가"),
            ["gnn", "2", "한국", "국어", "가"],
        )

    def test_committed_journal_restores_exact_legacy_hashes(self) -> None:
        original = _seed_legacy(self.topic)
        result = build_sparse_index(
            "fixture",
            self.docs,
            run_id="happy-run",
            timestamp="2026-08-05T00:00:00.000000Z",
        )
        manifest_path = cast(Path, result.manifest_path)
        manifest = cast(
            dict[str, object],
            cast(object, json.loads(manifest_path.read_text(encoding="utf-8"))),
        )
        self.assertEqual(manifest["phase"], "committed")
        self.assertEqual(manifest["run_id"], "happy-run")
        artifacts = cast(list[dict[str, object]], manifest["artifacts"])
        self.assertEqual(len(artifacts), 3)
        for row in artifacts:
            self.assertIn("original_path", row)
            self.assertIn("quarantine_path", row)
            self.assertIn("size", row)
            self.assertIn("sha256", row)
            self.assertIn("reason", row)
            self.assertEqual(
                row["timestamp"],
                "2026-08-05T00:00:00.000000Z",
            )
        self.assertIsInstance(manifest["restore_command"], list)
        activation = cast(
            dict[str, object],
            manifest["activation_intent"],
        )
        self.assertEqual(activation["final_destination"], "_search_index.json")
        self.assertIsInstance(activation["prior_active"], dict)
        self.assertIsInstance(activation["v2_temp"], dict)
        self.assertEqual(
            cast(dict[str, object], activation["v2_temp"])["sha256"],
            _sha256(result.active_path),
        )
        self.assertEqual((self.topic / "user-sentinel.txt").read_text(), "preserve-me")
        self.assertFalse((self.topic / "_search_index_emb.bin").exists())
        self.assertFalse((self.topic / "_embedding_cache.json").exists())
        phases = [
            cast(dict[str, object], row)["phase"]
            for row in cast(list[object], manifest["history"])
        ]
        self.assertEqual(
            phases,
            [
                "prepared",
                "legacy_moved:1",
                "legacy_moved:2",
                "temp_fsynced",
                "activation_intent",
                "old_backed_up",
                "replaced",
                "committed",
            ],
        )

        restored = restore_transaction(manifest_path, self.docs)
        restored_again = restore_transaction(manifest_path, self.docs)
        self.assertEqual(restored.phase, "restored")
        self.assertEqual(restored_again.phase, "restored")
        for name, digest in original.items():
            self.assertEqual(_sha256(self.topic / name), digest)

    def test_every_failpoint_recovers_twice_without_partial_active_v2(self) -> None:
        self.assertEqual(
            FAILPOINTS,
            (
                "after_prepared",
                "after_legacy_move:1",
                "after_legacy_move:2",
                "after_temp_fsync",
                "after_activation_intent",
                "after_old_backup",
                "after_replace",
                "before_commit",
            ),
        )
        for failpoint in FAILPOINTS:
            with self.subTest(failpoint=failpoint):
                case = Path(self.temporary.name) / (
                    "case-" + failpoint.replace(":", "-")
                )
                docs = case / "docs"
                _write_source(docs)
                topic = docs / "fixture"
                original = _seed_legacy(topic)
                environment = os.environ.copy()
                environment["PYTHONUTF8"] = "1"
                environment["PAPER_CURATION_TESTING"] = "1"
                environment["PAPER_CURATION_FAILPOINT"] = failpoint
                code = (
                    "import sys;"
                    f"sys.path.insert(0,{str(ROOT)!r});"
                    "from pipeline.build_search_index import main;"
                    "raise SystemExit(main())"
                )
                completed = subprocess.run(
                    [
                        str(PYTHON),
                        "-c",
                        code,
                        "--topic",
                        "fixture",
                        "--docs-dir",
                        str(docs),
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 86, completed.stderr)
                manifest = _latest_manifest(topic)
                first = recover_transaction(manifest, docs)
                first_manifest = manifest.read_bytes()
                second = recover_transaction(manifest, docs)
                self.assertEqual(first.phase, "rolled_back")
                self.assertEqual(second.phase, "rolled_back")
                self.assertEqual(manifest.read_bytes(), first_manifest)
                for name, digest in original.items():
                    self.assertEqual(_sha256(topic / name), digest)
                active = cast(
                    dict[str, object],
                    cast(
                        object,
                        json.loads(
                            (topic / "_search_index.json").read_text(
                                encoding="utf-8"
                            )
                        ),
                    ),
                )
                self.assertNotEqual(active.get("schema"), SPARSE_SCHEMA)

    def test_preflight_failure_mutates_nothing(self) -> None:
        original = _seed_legacy(self.topic)

        class Unsupported:
            def __init__(self, reason: str) -> None:
                self.reason: str = reason

            def preflight(self, _topic: Path, _paths: list[Path]) -> None:
                raise DurabilityError(self.reason)

            def durable_write(self, _path: Path, _data: bytes) -> None:
                raise AssertionError("write after failed preflight")

            def move(self, _source: Path, _target: Path) -> None:
                raise AssertionError("move after failed preflight")

        for reason in (
            "write-through-primitive-unavailable",
            "ntfs-required:REFS",
            "same-volume-required",
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(DurabilityError, reason):
                    _ = build_sparse_index(
                        "fixture",
                        self.docs,
                        durability=cast(
                            DurableIO,
                            cast(object, Unsupported(reason)),
                        ),
                    )
        for name, digest in original.items():
            self.assertEqual(_sha256(self.topic / name), digest)
        self.assertFalse((self.topic / ".curation-quarantine").exists())

    def test_invalid_legacy_and_out_of_topic_manifest_are_refused(self) -> None:
        active = self.topic / "_search_index.json"
        _ = active.write_text("user sentinel", encoding="utf-8")
        before = _sha256(active)
        with self.assertRaises(SparseIndexError):
            _ = build_sparse_index("fixture", self.docs)
        self.assertEqual(_sha256(active), before)
        self.assertFalse((self.topic / ".curation-quarantine").exists())

        outside = Path(self.temporary.name) / "manifest.json"
        _ = outside.write_text("{}", encoding="utf-8")
        for operation in (recover_transaction, restore_transaction):
            with self.assertRaises(SparseIndexError):
                _ = operation(outside, self.docs)
        with self.assertRaises(SparseIndexError):
            _ = purge_transaction(
                outside.resolve(),
                self.docs,
                confirmation="outside",
                manifest_sha256=_sha256(outside),
            )

        environment = os.environ.copy()
        environment["PAPER_CURATION_TESTING"] = "1"
        environment["PAPER_CURATION_FAILPOINT"] = "unknown-point"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(SparseIndexError):
                _ = build_sparse_index("fixture", self.docs)
        self.assertFalse((self.topic / ".curation-quarantine").exists())

    def test_purge_requires_committed_exact_manifest_and_confirmation(self) -> None:
        _ = _seed_legacy(self.topic)
        result = build_sparse_index(
            "fixture",
            self.docs,
            run_id="purge-run",
        )
        manifest = cast(Path, result.manifest_path)
        with self.assertRaises(SparseIndexError):
            _ = purge_transaction(
                manifest,
                self.docs,
                confirmation=None,
                manifest_sha256=_sha256(manifest),
            )
        with self.assertRaises(SparseIndexError):
            _ = purge_transaction(
                Path(manifest.name),
                self.docs,
                confirmation="purge-run",
                manifest_sha256=_sha256(manifest),
            )
        quarantined = sorted(manifest.parent.joinpath("legacy").iterdir())
        _ = quarantined[0].write_bytes(b"tampered")
        with self.assertRaises(SparseIndexError):
            _ = purge_transaction(
                manifest,
                self.docs,
                confirmation="purge-run",
                manifest_sha256=_sha256(manifest),
            )

    def test_purge_success_is_explicit_and_terminal(self) -> None:
        _ = _seed_legacy(self.topic)
        result = build_sparse_index(
            "fixture",
            self.docs,
            run_id="purge-success",
        )
        manifest = cast(Path, result.manifest_path)
        purged = purge_transaction(
            manifest,
            self.docs,
            confirmation="purge-success",
            manifest_sha256=_sha256(manifest),
        )
        self.assertEqual(purged.phase, "purged")
        self.assertEqual(
            list(manifest.parent.joinpath("legacy").iterdir()),
            [],
        )
        with self.assertRaises(SparseIndexError):
            _ = purge_transaction(
                manifest,
                self.docs,
                confirmation="purge-success",
                manifest_sha256=_sha256(manifest),
            )

    def test_restored_and_transitional_journals_are_not_purgeable(self) -> None:
        _ = _seed_legacy(self.topic)
        transitional = self.topic / "_search_index.bm25-v2.json"
        _ = transitional.write_text(
            json.dumps(
                {
                    "documents": [],
                    "postings": {},
                    "schema": SPARSE_SCHEMA,
                    "schema_version": 2,
                    "source": {},
                    "topic": "fixture",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        result = build_sparse_index(
            "fixture",
            self.docs,
            run_id="restore-then-refuse",
        )
        manifest = cast(Path, result.manifest_path)
        journal = cast(
            dict[str, object],
            cast(object, json.loads(manifest.read_text(encoding="utf-8"))),
        )
        reasons = {
            str(cast(dict[str, object], row)["reason"])
            for row in cast(list[object], journal["artifacts"])
        }
        self.assertIn("transitional-sparse-sidecar", reasons)
        _ = restore_transaction(manifest, self.docs)
        with self.assertRaises(SparseIndexError):
            _ = purge_transaction(
                manifest,
                self.docs,
                confirmation="restore-then-refuse",
                manifest_sha256=_sha256(manifest),
            )
    def test_cli_defaults_to_bm25_and_rejects_other_modes(self) -> None:
        self.assertEqual(
            index_main(
                [
                    "--topic",
                    "fixture",
                    "--docs-dir",
                    str(self.docs),
                ]
            ),
            0,
        )
        with self.assertRaises(SystemExit):
            _ = index_main(
                [
                    "--topic",
                    "fixture",
                    "--docs-dir",
                    str(self.docs),
                    "--mode",
                    "dense",
                ]
            )


if __name__ == "__main__":
    _ = unittest.main()
