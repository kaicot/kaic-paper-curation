"""Read-only BM25 querying for sparse-index-v2 artifacts."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Final, cast


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.sparse_index import (  # noqa: E402
    ACTIVE_NAME,
    SPARSE_SCHEMA,
    SparseIndexError,
    cross_sparse_payload,
    sparse_payload,
    tokenize,
    validate_sparse_index_payload,
)


QUERY_SCHEMA: Final = "sparse-query-v1"


class SparseQueryError(RuntimeError):
    pass


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SparseQueryError(f"duplicate-json-key:{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise SparseQueryError(f"invalid-json-constant:{value}")


def _load_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise SparseQueryError("index-file-required")
    try:
        raw = cast(
            object,
            json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_constant,
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SparseQueryError(f"invalid-index-json:{error}") from error
    if not isinstance(raw, dict):
        raise SparseQueryError("index-root-must-be-object")
    return cast(dict[str, object], raw)


def _contained(root: Path, path: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve(strict=False)
    if (
        resolved_path != resolved_root
        and resolved_root not in resolved_path.parents
    ):
        raise SparseQueryError("index-path-outside-docs")


def _topic_freshness(
    selected_topic: str,
    docs_dir: Path,
    value: dict[str, object],
) -> tuple[str, str]:
    try:
        expected = (
            cross_sparse_payload(
                cast(list[str], value.get("topics", [])),
                docs_dir,
            )
            if selected_topic == "_cross"
            else sparse_payload(
                selected_topic,
                docs_dir / "papers" / "_papers_index.json",
            )
        )
        _ = validate_sparse_index_payload(expected, selected_topic)
    except (OSError, SparseIndexError, RuntimeError, ValueError) as error:
        return "stale", f"source-unavailable:{error}"
    if (
        expected.get("source_fingerprint")
        != value.get("source_fingerprint")
    ):
        return "stale", "source-fingerprint-mismatch"
    if _canonical_json(expected) != _canonical_json(value):
        return "corrupt", "canonical-payload-mismatch"
    return "fresh", ""


def _response(
    status: str,
    topic: str,
    query: str,
    *,
    code: str | None = None,
    message: str | None = None,
    results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "dense_score": 0.0,
        "mode": "bm25",
        "query": query,
        "results": results or [],
        "schema": QUERY_SCHEMA,
        "schema_version": 1,
        "status": status,
        "topic": topic,
    }
    if code is not None:
        payload["code"] = code
    if message is not None:
        payload["message"] = message
    if status != "ok":
        payload["rebuild_command"] = [
            sys.executable,
            "pipeline/build_search_index.py",
            "--topic",
            topic,
            "--mode",
            "bm25",
        ]
    return payload


def query_search_index(
    topic: str | None = "_cross",
    query: str = "",
    *,
    mode: str = "bm25",
    top_k: int = 10,
    docs_dir: str | Path | None = None,
    **_retired_options: object,
) -> dict[str, object]:
    selected_topic = topic or "_cross"
    if mode != "bm25":
        return _response(
            "unsupported-mode",
            selected_topic,
            query,
            code="bm25-only",
            message=f"retrieval mode {mode!r} is unavailable",
        )
    if not query.strip():
        return _response(
            "invalid-query",
            selected_topic,
            query,
            code="query-required",
        )
    query_terms = sorted(set(tokenize(query)))
    if not query_terms:
        return _response(
            "invalid-query",
            selected_topic,
            query,
            code="empty-query-terms",
        )
    if (
        isinstance(top_k, bool)
        or top_k < 1
        or top_k > 100
    ):
        return _response(
            "invalid-query",
            selected_topic,
            query,
            code="top-k-out-of-range",
        )
    docs_root = (
        Path(docs_dir).resolve()
        if docs_dir is not None
        else (ROOT / "docs").resolve()
    )
    if (
        not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", selected_topic)
        and selected_topic != "_cross"
    ) or ".." in selected_topic:
        return _response(
            "invalid-index",
            selected_topic,
            query,
            code="invalid-topic",
        )
    index_path = docs_root / selected_topic / ACTIVE_NAME
    try:
        _contained(docs_root, index_path)
        if not index_path.exists():
            return _response(
                "missing-index",
                selected_topic,
                query,
                code="sparse-index-missing",
            )
        value = _load_json(index_path)
        if (
            value.get("schema") != SPARSE_SCHEMA
            or value.get("schema_version") != 2
        ):
            return _response(
                "unsupported-index",
                selected_topic,
                query,
                code="sparse-index-v2-required",
            )
        documents, postings = validate_sparse_index_payload(
            value,
            selected_topic,
        )
        if index_path.read_bytes() != _canonical_json(value):
            return _response(
                "invalid-index",
                selected_topic,
                query,
                code="noncanonical-index",
            )
        freshness, reason = _topic_freshness(
            selected_topic,
            docs_root,
            value,
        )
        if freshness == "stale":
            return _response(
                "stale-index",
                selected_topic,
                query,
                code=reason,
            )
        if freshness == "corrupt":
            return _response(
                "invalid-index",
                selected_topic,
                query,
                code=reason,
            )
    except (SparseQueryError, SparseIndexError) as error:
        return _response(
            "invalid-index",
            selected_topic,
            query,
            code=str(error),
        )
    except (OSError, ValueError) as error:
        return _response(
            "invalid-index",
            selected_topic,
            query,
            code=f"index-io:{error}",
        )
    if not documents:
        return _response(
            "empty-index",
            selected_topic,
            query,
            code="no-documents",
        )
    term_rows: dict[str, dict[int, int]] = {
        term: {
            document_id: frequency
            for document_id, frequency in postings.get(term, [])
        }
        for term in query_terms
        if term in postings
    }
    average_value = value["average_document_length"]
    if not isinstance(average_value, (int, float)):
        raise AssertionError("validated average length must be numeric")
    average_length = float(average_value)
    document_count = len(documents)
    scored: list[tuple[float, int, list[str]]] = []
    for document_id, document in enumerate(documents):
        length = cast(int, document["length"])
        score = 0.0
        matched: list[str] = []
        for term, frequencies in term_rows.items():
            frequency = frequencies.get(document_id)
            if frequency is None:
                continue
            matched.append(term)
            document_frequency = len(postings[term])
            inverse_frequency = math.log(
                1
                + (
                    document_count
                    - document_frequency
                    + 0.5
                )
                / (document_frequency + 0.5)
            )
            denominator = frequency + 1.5 * (
                0.25
                + 0.75
                * (length / average_length if average_length else 0.0)
            )
            score += inverse_frequency * frequency * 2.5 / denominator
        if matched:
            scored.append((score, document_id, sorted(matched)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    results: list[dict[str, object]] = []
    for rank, (score, document_id, matched) in enumerate(scored[:top_k], 1):
        document = documents[document_id]
        result: dict[str, object] = {
            "bm25_score": score,
            "dense_score": 0.0,
            "document_id": document_id,
            "matched_terms": matched,
            "rank": rank,
            "score": score,
            "slug": document["slug"],
            "title": document["title"],
        }
        if "topics" in document:
            result["topics"] = document["topics"]
        results.append(result)
    response = _response(
        "ok",
        selected_topic,
        query,
        results=results,
    )
    response["index_source_fingerprint"] = value["source_fingerprint"]
    response["query_terms"] = query_terms
    return response


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query a local sparse-index-v2 artifact with BM25",
    )
    _ = parser.add_argument("--topic")
    _ = parser.add_argument("--query", required=True)
    _ = parser.add_argument("--mode", default="bm25")
    _ = parser.add_argument("--top-k", type=int, default=10)
    _ = parser.add_argument("--docs-dir", type=Path)
    _ = parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    payload = query_search_index(
        cast(str | None, arguments.topic),
        cast(str, arguments.query),
        mode=cast(str, arguments.mode),
        top_k=cast(int, arguments.top_k),
        docs_dir=cast(Path | None, arguments.docs_dir),
    )
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if cast(bool, arguments.json):
        print(rendered)
    elif payload["status"] == "ok":
        for result in cast(list[dict[str, object]], payload["results"]):
            score = cast(float, result["score"])
            print(
                f"{result['rank']}. {result['slug']} {score:.6f}"
            )
    else:
        print(rendered, file=sys.stderr)
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
