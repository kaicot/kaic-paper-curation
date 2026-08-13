"""Build and transactionally activate the local sparse-index-v2 artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.sparse_index import (  # noqa: E402
    SparseIndexError,
    build_sparse_index,
    purge_transaction,
    recover_transaction,
    restore_transaction,
)


DOCS_DIR = ROOT / "docs"
PAPERS_DIR = DOCS_DIR / "papers"


def source_fingerprint(
    topic: str,
    slugs: list[str],
    *,
    docs_dir: Path | None = None,
    papers_dir: Path | None = None,
) -> tuple[str, int]:
    """Compatibility fingerprint for a retired index during migration."""
    docs_root = Path(docs_dir) if docs_dir is not None else DOCS_DIR
    papers_root = Path(papers_dir) if papers_dir is not None else PAPERS_DIR
    files: list[tuple[str, Path]] = []
    for slug in sorted(set(slugs)):
        for name in ("review.md", "notes.md"):
            path = papers_root / slug / name
            if path.exists():
                files.append((f"papers/{slug}/{name}", path))
    notes_root = docs_root / "notes" / topic
    if notes_root.exists():
        for path in sorted(notes_root.rglob("*.md")):
            if not path.name.startswith("_"):
                relative = path.relative_to(notes_root).as_posix()
                files.append((f"notes/{topic}/{relative}", path))
    digest = hashlib.sha256()
    for relative, path in files:
        stat = path.stat()
        digest.update(
            (
                f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest(), len(files)


@dataclass(frozen=True, slots=True)
class Arguments:
    action: str
    confirmation: str | None
    docs_dir: Path
    manifest: Path | None
    manifest_sha256: str | None
    mode: str
    topic: str | None


def parse_arguments(argv: list[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--topic")
    _ = parser.add_argument("--docs-dir", type=Path, default=DOCS_DIR)
    _ = parser.add_argument("--mode", choices=("bm25",), default="bm25")
    actions = parser.add_mutually_exclusive_group()
    _ = actions.add_argument("--recover", action="store_true")
    _ = actions.add_argument("--restore", action="store_true")
    _ = actions.add_argument("--purge", action="store_true")
    _ = parser.add_argument("--manifest", type=Path)
    _ = parser.add_argument("--manifest-sha256")
    _ = parser.add_argument("--confirm-purge")
    namespace = parser.parse_args(argv)
    action = (
        "recover"
        if cast(bool, namespace.recover)
        else "restore"
        if cast(bool, namespace.restore)
        else "purge"
        if cast(bool, namespace.purge)
        else "build"
    )
    return Arguments(
        action=action,
        confirmation=cast(str | None, namespace.confirm_purge),
        docs_dir=cast(Path, namespace.docs_dir).resolve(),
        manifest=cast(Path | None, namespace.manifest),
        manifest_sha256=cast(str | None, namespace.manifest_sha256),
        mode=cast(str, namespace.mode),
        topic=cast(str | None, namespace.topic),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        if arguments.action == "build":
            if arguments.topic is None:
                raise SparseIndexError("topic-required")
            result = build_sparse_index(
                arguments.topic,
                arguments.docs_dir,
            )
        else:
            if arguments.manifest is None:
                raise SparseIndexError("manifest-required")
            manifest = arguments.manifest.resolve()
            if arguments.action == "recover":
                result = recover_transaction(
                    manifest,
                    arguments.docs_dir,
                )
            elif arguments.action == "restore":
                result = restore_transaction(
                    manifest,
                    arguments.docs_dir,
                )
            else:
                result = purge_transaction(
                    manifest,
                    arguments.docs_dir,
                    confirmation=arguments.confirmation,
                    manifest_sha256=arguments.manifest_sha256,
                )
    except SparseIndexError as error:
        print(f"Sparse index denied: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "active_path": str(result.active_path),
                "manifest_path": (
                    str(result.manifest_path)
                    if result.manifest_path is not None
                    else None
                ),
                "phase": result.phase,
                "reused": result.reused,
                "status": "ok",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
