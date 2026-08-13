#!/usr/bin/env python3
"""Atomically mirror retrieval-evaluation inputs outside macOS Documents TCC."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

LABEL = "dev.jehyunlee.paper-curation.retrieval-eval"
RUNTIME_FILES = (
    "evaluate_retrieval.py",
    "query_search_index.py",
    "sparse_index.py",
)
EVAL_FILES = (
    "retrieval_queries.jsonl",
    "retrieval_queries.meta.json",
    "retrieval_baseline.json",
)
INDEX_FILES = ("_search_index.json",)


def _collections(query_path: Path) -> list[str]:
    names: set[str] = set()
    for number, line in enumerate(query_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row_value = cast(object, json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"query line {number}: invalid JSON") from exc
        row = (
            cast(dict[str, object], row_value)
            if isinstance(row_value, dict)
            else {}
        )
        collection = row.get("collection")
        if not isinstance(collection, str) or not collection:
            raise ValueError(f"query line {number}: collection must be a non-empty string")
        names.add(collection)
    if not names:
        raise ValueError("query set contains no collections")
    return sorted(names)


def _snapshot_sources(
    docs: Path,
    requested: list[str],
) -> tuple[list[str], list[Path]]:
    collections = set(requested)
    pending = list(requested)
    review_paths: set[Path] = set()
    while pending:
        collection = pending.pop()
        index_path = docs / collection / "_search_index.json"
        try:
            index_value = cast(
                object,
                json.loads(index_path.read_text(encoding="utf-8")),
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid sparse index: {index_path}") from exc
        if not isinstance(index_value, dict):
            raise ValueError(f"invalid sparse index root: {index_path}")
        index = cast(dict[str, object], index_value)
        if collection == "_cross":
            topics = index.get("topics")
            if not isinstance(topics, list) or not all(
                isinstance(topic, str) and topic
                for topic in cast(list[object], topics)
            ):
                raise ValueError(f"invalid cross index topics: {index_path}")
            for topic in cast(list[str], topics):
                if topic not in collections:
                    collections.add(topic)
                    pending.append(topic)
            continue
        source_value = index.get("source")
        source = (
            cast(dict[str, object], source_value)
            if isinstance(source_value, dict)
            else {}
        )
        reviews = source.get("reviews")
        if not isinstance(reviews, list):
            raise ValueError(f"invalid sparse review sources: {index_path}")
        for row in cast(list[object], reviews):
            review_row = (
                cast(dict[str, object], row)
                if isinstance(row, dict)
                else {}
            )
            relative = review_row.get("path")
            if not isinstance(relative, str):
                raise ValueError(f"invalid sparse review path: {index_path}")
            candidate = Path(relative)
            resolved = (docs / candidate).resolve()
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or docs.resolve() not in resolved.parents
                or resolved.is_symlink()
                or not resolved.is_file()
            ):
                raise ValueError(f"unsafe sparse review path: {relative}")
            review_paths.add(candidate)
    return sorted(collections), sorted(review_paths)


def refresh_snapshot(project_root: str | Path, output: str | Path) -> Path:
    root, destination = Path(project_root).resolve(), Path(output).expanduser().resolve()
    pipeline, eval_dir, docs = root / "pipeline", root / "pipeline" / "eval", root / "docs"
    collections = _collections(eval_dir / "retrieval_queries.jsonl")
    collections, review_paths = _snapshot_sources(docs, collections)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    backup = destination.with_name(f".{destination.name}.previous")
    try:
        (temporary / "pipeline" / "eval").mkdir(parents=True)
        for name in RUNTIME_FILES:
            _ = shutil.copy2(pipeline / name, temporary / "pipeline" / name)
        for name in EVAL_FILES:
            _ = shutil.copy2(
                eval_dir / name,
                temporary / "pipeline" / "eval" / name,
            )
        for collection in collections:
            target = temporary / "docs" / collection
            target.mkdir(parents=True)
            for name in INDEX_FILES:
                _ = shutil.copy2(docs / collection / name, target / name)
        papers_index = docs / "papers" / "_papers_index.json"
        papers_target = temporary / "docs" / "papers"
        papers_target.mkdir(parents=True)
        _ = shutil.copy2(papers_index, papers_target / papers_index.name)
        for relative in review_paths:
            target = temporary / "docs" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(docs / relative, target)

        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(temporary, destination)
        except BaseException:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    print(f"retrieval evaluation snapshot: {len(collections)} collections -> {destination}")
    return destination


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Refresh the TCC-safe retrieval evaluation snapshot")
    _ = parser.add_argument("--project-root", default=str(root))
    _ = parser.add_argument(
        "--output",
        default=str(Path.home() / "Library" / "Application Support" / "paper-curation" / "retrieval-eval"),
    )
    _ = parser.add_argument("--if-installed", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if cast(bool, args.if_installed):
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if not plist.exists():
            print("retrieval evaluation snapshot: skipped (LaunchAgent not installed)")
            return 0
    try:
        _ = refresh_snapshot(
            cast(str, args.project_root),
            cast(str, args.output),
        )
        return 0
    except (OSError, ValueError) as exc:
        print(f"retrieval evaluation snapshot error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
