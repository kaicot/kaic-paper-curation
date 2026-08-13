#!/usr/bin/env python3
"""Tests for fingerprint-based search-index deploy freshness."""
from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import pipeline.prepare_deploy as deploy
from pipeline.sparse_index import build_sparse_index


class SearchIndexFreshnessTests(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    root: Path = Path()
    docs: Path = Path()
    papers: Path = Path()
    topic: str = ""
    stack: contextlib.ExitStack = contextlib.ExitStack()

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.papers = self.docs / "papers"
        self.topic = "demo"
        _ = (self.docs / self.topic).mkdir(parents=True)
        _ = (self.papers / "p1").mkdir(parents=True)
        _ = (self.papers / "p1" / "review.md").write_text("review v1", encoding="utf-8")
        _ = (self.papers / "_papers_index.json").write_text(
            json.dumps(
                [
                    {
                        "slug": "p1",
                        "title": "Paper",
                        "topics": [self.topic],
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.stack = contextlib.ExitStack()
        _ = self.stack.enter_context(patch.object(deploy, "DOCS_DIR", self.docs))
        _ = self.stack.enter_context(patch.object(deploy, "PAPERS_DIR", str(self.papers)))

    @override
    def tearDown(self) -> None:
        self.stack.close()
        self.tmp.cleanup()

    def write_index(self, fingerprint: str | None = None) -> None:
        data = {
            "papers": {"p1": {"title": "Paper"}},
            "emb_file": "_search_index_emb.bin",
        }
        if fingerprint is not None:
            data["source_fingerprint"] = fingerprint
        topic_dir = self.docs / self.topic
        _ = (topic_dir / "_search_index.json").write_text(json.dumps(data), encoding="utf-8")
        _ = (topic_dir / "_search_index_emb.bin").write_bytes(b"\x00")

    def test_retired_index_is_explicitly_stale(self):
        self.write_index()
        result = deploy.search_index_freshness(self.topic)
        self.assertFalse(result["fresh"])
        self.assertEqual(result["reason"], "retired search index schema")

    def test_matching_sparse_fingerprint_is_fresh(self):
        _ = build_sparse_index(self.topic, self.docs)
        self.assertTrue(deploy.search_index_freshness(self.topic)["fresh"])

    def test_review_content_change_is_stale_and_preflight_blocks(self):
        _ = build_sparse_index(self.topic, self.docs)
        _ = (self.papers / "p1" / "review.md").write_text("review v2 changed", encoding="utf-8")
        result = deploy.search_index_freshness(self.topic)
        self.assertFalse(result["fresh"])
        with self.assertRaises(SystemExit):
            deploy.preflight_search_indexes([self.topic])

    def test_mtime_only_change_remains_fresh(self):
        _ = build_sparse_index(self.topic, self.docs)
        review = self.papers / "p1" / "review.md"
        review.touch()
        self.assertTrue(deploy.search_index_freshness(self.topic)["fresh"])


if __name__ == "__main__":
    _ = unittest.main()
