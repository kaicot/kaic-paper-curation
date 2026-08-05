"""Unavailable secondary capability boundary: prepare-deploy."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Never, cast

_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pipeline").is_dir()
)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.secondary_capability_guard import (  # noqa: E402
    cli as _capability_cli,
    deny as _deny,
)

CAPABILITY_NAME = "prepare-deploy"
DOCS_DIR = _ROOT / "docs"
PAPERS_DIR = DOCS_DIR / "papers"


def _search_index_freshness(topic: str) -> dict[str, object]:
    topic_dir = Path(DOCS_DIR) / topic
    index_path = topic_dir / "_search_index.json"
    if not index_path.exists():
        return {"topic": topic, "fresh": False, "reason": "index JSON missing"}
    try:
        index = cast(
            object,
            json.loads(index_path.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError) as error:
        return {
            "topic": topic,
            "fresh": False,
            "reason": f"invalid index JSON: {error}",
        }
    if not isinstance(index, dict):
        return {"topic": topic, "fresh": False, "reason": "invalid index shape"}
    value = cast(dict[str, object], index)
    if (
        value.get("schema") == "paper-curation-sparse-index-v2"
        and value.get("schema_version") == 2
    ):
        expected = value.get("source_fingerprint")
        if not isinstance(expected, str):
            return {
                "topic": topic,
                "fresh": False,
                "reason": "sparse source fingerprint missing",
            }
        from pipeline.sparse_index import current_source_sha256

        try:
            actual, source_count = current_source_sha256(
                topic,
                Path(DOCS_DIR),
            )
        except (OSError, ValueError, RuntimeError) as error:
            return {
                "topic": topic,
                "fresh": False,
                "reason": f"sparse source validation failed: {error}",
            }
        fresh = actual == expected
        return {
            "topic": topic,
            "fresh": fresh,
            "reason": "" if fresh else "sparse source content changed",
            "source_file_count": source_count + 1,
        }
    return {
        "topic": topic,
        "fresh": False,
        "reason": "retired search index schema",
    }


def _preflight_search_indexes(topics: list[str] | None = None) -> None:
    selected = topics or []
    stale = [
        result
        for result in (_search_index_freshness(topic) for topic in selected)
        if result["fresh"] is False
    ]
    if stale:
        raise SystemExit("Refusing to publish: stale search index")


search_index_freshness = _search_index_freshness
preflight_search_indexes = _preflight_search_indexes


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
