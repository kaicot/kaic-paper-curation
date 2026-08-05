"""Unavailable secondary capability boundary: prepare-deploy."""

from __future__ import annotations

import json
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

CAPABILITY_NAME = "prepare-deploy"
DOCS_DIR = _ROOT / "docs"
PAPERS_DIR = DOCS_DIR / "papers"


def _search_index_freshness(topic: str) -> dict[str, object]:
    topic_dir = Path(DOCS_DIR) / topic
    index_path = topic_dir / "_search_index.json"
    if not index_path.exists():
        return {"topic": topic, "fresh": False, "reason": "index JSON missing"}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "topic": topic,
            "fresh": False,
            "reason": f"invalid index JSON: {error}",
        }
    if not isinstance(index, dict):
        return {"topic": topic, "fresh": False, "reason": "invalid index shape"}
    sidecar_name = index.get("emb_file") or "_search_index_emb.bin"
    sidecar = topic_dir / str(sidecar_name)
    if not sidecar.exists():
        return {
            "topic": topic,
            "fresh": False,
            "reason": f"index sidecar missing: {sidecar_name}",
        }
    expected = index.get("source_fingerprint")
    if not expected:
        return {
            "topic": topic,
            "fresh": None,
            "reason": "index predates source fingerprint",
        }
    from pipeline.build_search_index import source_fingerprint

    papers = index.get("papers")
    slugs = list(papers) if isinstance(papers, dict) else []
    actual, source_count = source_fingerprint(
        topic,
        slugs,
        docs_dir=Path(DOCS_DIR),
        papers_dir=Path(PAPERS_DIR),
    )
    fresh = actual == expected
    return {
        "topic": topic,
        "fresh": fresh,
        "reason": "" if fresh else "indexed source files changed",
        "source_file_count": source_count,
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
