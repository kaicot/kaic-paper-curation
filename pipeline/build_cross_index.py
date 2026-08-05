"""Build a deterministic sparse v2 index across selected topics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.sparse_index import (  # noqa: E402
    ACTIVE_NAME,
    SparseIndexError,
    build_cross_sparse_index,
)


DOCS_DIR = ROOT / "docs"
SEARCH_INDEX = ACTIVE_NAME
EMB_BIN = "_search_index_emb.bin"


def build_cross_index(
    topics: list[str],
    *,
    docs_dir: Path | None = None,
) -> Path:
    root = Path(docs_dir) if docs_dir is not None else Path(DOCS_DIR)
    result = build_cross_sparse_index(topics, root)
    return result.active_path


def merge_indexes(
    topics: list[str],
) -> tuple[dict[str, object], bytes, dict[str, object]]:
    """Compatibility adapter returning the sparse payload and provenance."""
    path = build_cross_index(topics, docs_dir=Path(DOCS_DIR))
    value = cast(
        dict[str, object],
        cast(object, json.loads(path.read_text(encoding="utf-8"))),
    )
    source = cast(dict[str, object], value.get("source", {}))
    return value, b"", source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("topics", nargs="+")
    _ = parser.add_argument("--docs-dir", type=Path, default=DOCS_DIR)
    arguments = parser.parse_args(argv)
    try:
        output = build_cross_index(
            cast(list[str], arguments.topics),
            docs_dir=cast(Path, arguments.docs_dir),
        )
    except SparseIndexError as error:
        print(f"Cross sparse index denied: {error}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
