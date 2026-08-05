"""Unavailable secondary capability boundary: build-search-index."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Never

_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pipeline").is_dir()
)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.secondary_capability_guard import (  # noqa: E402
    CAPABILITY_STATUS as SECONDARY_CAPABILITY_STATUS,
    cli as _capability_cli,
    deny as _deny,
)

CAPABILITY_NAME = "build-search-index"
DOCS_DIR = _ROOT / "docs"
PAPERS_DIR = DOCS_DIR / "papers"


def source_fingerprint(
    topic: str,
    slugs: list[str],
    *,
    docs_dir: Path | None = None,
    papers_dir: Path | None = None,
) -> tuple[str, int]:
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


def _unavailable(*_args: object, **_kwargs: object) -> Never:
    _ = (_args, _kwargs)
    return _deny(CAPABILITY_NAME)


def __getattr__(name: str):
    if name.startswith("__"):
        raise AttributeError(name)
    return _unavailable


def main(argv: list[str] | None = None) -> int:
    return _capability_cli(CAPABILITY_NAME, argv)


if __name__ == "__main__":
    raise SystemExit(main())
