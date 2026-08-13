#!/usr/bin/env python3
"""Deterministic, offline retrieval-quality evaluator for local search indexes."""
from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.query_search_index import query_search_index  # noqa: E402

SCHEMA_VERSION = 2
MAX_K = 10
FAILURE_K = 5


class EvaluationError(ValueError):
    """Invalid evaluator input or local index configuration."""


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 of a file's exact bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: str | Path, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvaluationError(f"cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid {label} JSON: {path}: {exc.msg}") from exc


def load_query_set(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """Load JSONL queries and hash normalized UTF-8 line endings."""
    query_path = Path(path)
    try:
        text = query_path.read_text(encoding="utf-8")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.splitlines()
    except (OSError, UnicodeError) as exc:
        raise EvaluationError(f"cannot read query set: {path}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    required = {"id", "query", "collection", "relevant_slugs"}
    optional = {"language", "category", "notes"}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise EvaluationError(f"query set line {number}: blank lines are not allowed")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"query set line {number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise EvaluationError(f"query set line {number}: row must be an object")
        missing = required - row.keys()
        unknown = row.keys() - required - optional
        if missing:
            raise EvaluationError(f"query set line {number}: missing required fields: {', '.join(sorted(missing))}")
        if unknown:
            raise EvaluationError(f"query set line {number}: unknown fields: {', '.join(sorted(unknown))}")
        for field in ("id", "query", "collection"):
            if not isinstance(row[field], str) or not row[field].strip():
                raise EvaluationError(f"query set line {number}: {field} must be a non-empty string")
        if row["id"] in seen_ids:
            raise EvaluationError(f"query set line {number}: duplicate id: {row['id']}")
        relevant = row["relevant_slugs"]
        if (not isinstance(relevant, list) or not relevant or
                any(not isinstance(slug, str) or not slug.strip() for slug in relevant)):
            raise EvaluationError(f"query set line {number}: relevant_slugs must be a non-empty string list")
        if len(set(relevant)) != len(relevant):
            raise EvaluationError(f"query set line {number}: relevant_slugs must not contain duplicates")
        for field in optional:
            if field in row and not isinstance(row[field], str):
                raise EvaluationError(f"query set line {number}: {field} must be a string")
        seen_ids.add(row["id"])
        rows.append(row)
    if not rows:
        raise EvaluationError("query set must contain at least one row")
    return rows, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def collection_paper_slugs(collection: str, docs_dir: str | Path | None) -> set[str]:
    """Read a collection's paper metadata without changing the local index."""
    root = Path(docs_dir) if docs_dir is not None else Path(__file__).resolve().parent.parent / "docs"
    index_path = root / collection / "_search_index.json"
    index = _read_json(index_path, "search index")
    if not isinstance(index, dict) or not isinstance(index.get("documents"), list):
        raise EvaluationError(f"invalid sparse index document metadata: {index_path}")
    slugs: list[str] = []
    for item in index["documents"]:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            raise EvaluationError(f"invalid sparse index document row: {index_path}")
        slugs.append(str(item["slug"]))
    if len(slugs) != len(set(slugs)) or slugs != sorted(slugs):
        raise EvaluationError(f"invalid sparse index slug ordering: {index_path}")
    return set(slugs)


def validate_relevant_slugs(rows: Iterable[Mapping[str, Any]], docs_dir: str | Path | None) -> dict[str, set[str]]:
    """Ensure each expected slug exists in its collection's papers metadata."""
    available: dict[str, set[str]] = {}
    absent: list[str] = []
    for row in rows:
        collection = str(row["collection"])
        if collection not in available:
            available[collection] = collection_paper_slugs(collection, docs_dir)
        missing = sorted(
            set(cast(Sequence[str], row["relevant_slugs"]))
            - available[collection]
        )
        if missing:
            absent.append(f"{row['id']} ({collection}): {', '.join(missing)}")
    if absent:
        raise EvaluationError("expected slug absent from collection metadata: " + "; ".join(absent))
    return available


def evaluate_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    docs_dir: str | Path | None = None,
    max_k: int = MAX_K,
) -> list[dict[str, Any]]:
    """Run each query through the local BM25-only contract."""
    if max_k < MAX_K:
        raise EvaluationError(f"max_k must be at least {MAX_K}")
    evaluations: list[dict[str, Any]] = []
    for row in rows:
        result = query_search_index(
            str(row["collection"]),
            str(row["query"]),
            top_k=max_k,
            docs_dir=docs_dir,
        )
        if result.get("status") != "ok":
            raise EvaluationError(
                f"query {row['id']} failed: "
                f"{result.get('status')}:{result.get('code', '')}"
            )
        result_rows = cast(
            list[dict[str, object]],
            result.get("results", []),
        )
        top_slugs = [str(item.get("slug", "")) for item in result_rows]
        ranks: dict[str, int | None] = {}
        relevant_slugs = cast(Sequence[str], row["relevant_slugs"])
        for slug in relevant_slugs:
            ranks[slug] = next((rank for rank, actual in enumerate(top_slugs, 1) if actual == slug), None)
        evaluations.append({"id": row["id"], "query": row["query"], "collection": row["collection"],
                            "relevant_slugs": list(relevant_slugs), "ranks": ranks,
                            "top_slugs": top_slugs[:max_k]})
    return evaluations


def compute_metrics(evaluations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute collection and aggregate recall/MRR metrics from evaluation rows."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in evaluations:
        grouped.setdefault(str(item["collection"]), []).append(item)

    def metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        count = len(items)
        recall5 = sum(sum(rank is not None and rank <= 5 for rank in item["ranks"].values()) /
                      len(item["relevant_slugs"]) for item in items) / count if count else 0.0
        recall10 = sum(sum(rank is not None and rank <= 10 for rank in item["ranks"].values()) /
                       len(item["relevant_slugs"]) for item in items) / count if count else 0.0
        mrr10 = sum(1 / min(ranks) if (ranks := [rank for rank in item["ranks"].values()
                                               if rank is not None and rank <= 10]) else 0.0
                    for item in items) / count if count else 0.0
        failures = [item for item in items
                    if any(rank is None or rank > FAILURE_K for rank in item["ranks"].values())]
        return {"query_count": count, "recall_at_5": recall5, "recall_at_10": recall10,
                "mrr_at_10": mrr10, "failure_count": len(failures)}

    return {"collections": {name: metrics(grouped[name]) for name in sorted(grouped)},
            "aggregate": metrics(evaluations)}


def build_report(evaluations: Sequence[Mapping[str, Any]], metrics: Mapping[str, Any], *,
                 query_set_sha256: str) -> dict[str, Any]:
    failures = [{"id": item["id"], "collection": item["collection"],
                 "expected_slugs": item["relevant_slugs"], "actual_top_slugs": item["top_slugs"],
                 "ranks": item["ranks"]}
                for item in evaluations
                if any(rank is None or rank > FAILURE_K for rank in item["ranks"].values())]
    return {"schema_version": SCHEMA_VERSION,
            "timestamp": _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "query_set_sha256": query_set_sha256, "retrieval_mode": "bm25",
            "collections": metrics["collections"], "aggregate": metrics["aggregate"],
            "queries": list(evaluations), "failures": failures}


def write_json_atomic(path: str | Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a JSON output, never exposing a partial report."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_report(path: str | Path, report: Mapping[str, Any]) -> None:
    write_json_atomic(path, report)


def write_baseline(path: str | Path, report: Mapping[str, Any]) -> None:
    """Write only comparable metrics and input identities, never query detail."""
    baseline = {"schema_version": SCHEMA_VERSION, "query_set_sha256": report["query_set_sha256"],
                "retrieval_mode": "bm25",
                "collections": report["collections"], "aggregate": report["aggregate"]}
    write_json_atomic(path, baseline)


def load_baseline(path: str | Path) -> dict[str, Any]:
    baseline = _read_json(path, "baseline")
    if isinstance(baseline, dict) and baseline.get("status") == "record-required":
        raise EvaluationError(
            "BM25 baseline must be recorded with --record-baseline"
        )
    if not isinstance(baseline, dict) or not isinstance(baseline.get("collections"), dict):
        raise EvaluationError("baseline must contain collection metrics")
    return baseline


def validate_baseline_identity(baseline: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    """Reject comparisons across different query sets or retrieval modes."""
    if baseline.get("schema_version") != SCHEMA_VERSION:
        raise EvaluationError("baseline schema_version does not match current evaluation")
    for field in ("query_set_sha256", "retrieval_mode"):
        if baseline.get(field) != report.get(field):
            raise EvaluationError(f"baseline {field} does not match current evaluation")


def validate_baseline_metrics(baseline: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    """Require a finite recall comparator for every evaluated collection."""
    prior = baseline.get("collections")
    if not isinstance(prior, dict):
        raise EvaluationError("baseline collections must be an object")
    for collection in report["collections"]:
        metrics = prior.get(collection)
        recall = metrics.get("recall_at_5") if isinstance(metrics, dict) else None
        if (not isinstance(recall, (int, float)) or isinstance(recall, bool)
                or not math.isfinite(recall) or not 0 <= recall <= 1):
            raise EvaluationError(
                f"baseline missing valid recall_at_5 for evaluated collection: {collection}"
            )


def strict_failures(report: Mapping[str, Any], baseline: Mapping[str, Any] | None, *,
                    min_recall_at_5: float, max_regression: float) -> list[str]:
    problems: list[str] = []
    prior = baseline.get("collections", {}) if baseline else {}
    for collection, metrics in report["collections"].items():
        recall = metrics["recall_at_5"]
        if recall < min_recall_at_5:
            problems.append(f"{collection}: recall_at_5 {recall:.6f} < {min_recall_at_5:.6f}")
        old = prior.get(collection)
        if isinstance(old, dict) and isinstance(old.get("recall_at_5"), (int, float)):
            if recall < old["recall_at_5"] - max_regression:
                problems.append(f"{collection}: recall_at_5 regression {old['recall_at_5'] - recall:.6f} > {max_regression:.6f}")
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate local sparse-index-v2 BM25 retrieval")
    parser.add_argument("--queries", required=True, help="JSONL query set")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--topic", help="evaluate rows for this collection")
    selection.add_argument("--all", action="store_true", help="evaluate all query rows")
    parser.add_argument("--docs-dir", help="docs directory containing collection indexes")
    parser.add_argument("--output", required=True, help="JSON report output")
    parser.add_argument("--failures", help="optional failures-only JSON output")
    parser.add_argument("--baseline", help="baseline JSON for regression comparison")
    parser.add_argument("--strict", action="store_true", help="return failure for quality gate violations")
    parser.add_argument("--record-baseline", action="store_true", help="write compact baseline to --baseline")
    parser.add_argument("--min-recall-at-5", type=float, default=0.95)
    parser.add_argument("--max-regression", type=float, default=0.025)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if (not 0 <= args.min_recall_at_5 <= 1
                or not math.isfinite(args.max_regression) or args.max_regression < 0):
            raise EvaluationError(
                "min-recall-at-5 must be 0..1 and max-regression must be finite and non-negative"
            )
        if args.record_baseline and not args.baseline:
            raise EvaluationError("--record-baseline requires --baseline")
        if args.record_baseline and args.topic:
            raise EvaluationError(
                "--record-baseline requires --all; topic baselines may not replace the shared baseline"
            )
        all_rows, query_hash = load_query_set(args.queries)
        rows = all_rows
        if args.topic:
            rows = [row for row in all_rows if row["collection"] == args.topic]
            if not rows:
                raise EvaluationError(f"no query rows for topic: {args.topic}")
        _ = validate_relevant_slugs(rows, args.docs_dir)
        evaluations = evaluate_rows(rows, docs_dir=args.docs_dir)
        metrics = compute_metrics(evaluations)
        report = build_report(
            evaluations,
            metrics,
            query_set_sha256=query_hash,
        )
        write_report(args.output, report)
        aggregate = report["aggregate"]
        print(
            "retrieval evaluation: "
            f"{aggregate['query_count']} queries, "
            f"recall@5={aggregate['recall_at_5']:.3f}, "
            f"recall@10={aggregate['recall_at_10']:.3f}, "
            f"mrr@10={aggregate['mrr_at_10']:.3f}, "
            f"failures={aggregate['failure_count']}"
        )
        if args.failures:
            write_json_atomic(args.failures, {"schema_version": SCHEMA_VERSION, "failures": report["failures"]})
        if args.record_baseline:
            write_baseline(args.baseline, report)
        baseline = load_baseline(args.baseline) if args.baseline and not args.record_baseline else None
        if baseline:
            validate_baseline_identity(baseline, report)
            validate_baseline_metrics(baseline, report)
        problems = strict_failures(report, baseline, min_recall_at_5=args.min_recall_at_5,
                                   max_regression=args.max_regression)
        if args.strict and problems:
            for problem in problems:
                print(f"quality gate failed: {problem}")
            return 1
        return 0
    except (EvaluationError, OSError, ValueError) as exc:
        print(f"evaluation configuration error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
