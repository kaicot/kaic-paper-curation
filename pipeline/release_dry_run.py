"""Pure dry-run plans and the final local artifact acceptance gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

from pipeline.sparse_index import (
    SparseIndexError,
    sparse_payload,
    validate_sparse_index_payload,
)


DEFAULT_VALIDATOR_STAGES = (
    "review",
    "geometry",
    "paper-index",
    "classification",
    "summary",
    "connection",
    "timeline",
    "html",
    "bm25",
    "topic-index",
    "rss",
    "moc",
)
DENIED_GENERATION_STAGES = (
    "classification",
    "connection",
    "geometry",
    "moc",
    "review",
    "summary",
    "timeline",
)
FORBIDDEN_COUNTERS = {
    "auth": 0,
    "children": 0,
    "credentials": 0,
    "deploy": 0,
    "egress": 0,
    "git": 0,
    "hashes": 0,
    "writes": 0,
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOPIC = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SLUG = re.compile(r"[0-9][0-9A-Za-z_.-]{0,159}")


class ArtifactValidationError(RuntimeError):
    """Typed failure for one of the twelve required artifact stages."""

    def __init__(self, stage: str, code: str, path: Path | None = None) -> None:
        self.stage: str = stage
        self.code: str = code
        self.path: Path | None = path
        super().__init__(f"{stage}:{code}")


def canonical_json(value: object) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def emit(value: object) -> None:
    _ = sys.stdout.write(canonical_json(value))
    _ = sys.stdout.flush()


def _duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-json-key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite-json:{value}")


def _regular(path: Path, stage: str) -> Path:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ArtifactValidationError(stage, "regular-file-required", path)
    return path


def _read_text(path: Path, stage: str) -> str:
    try:
        return _regular(path, stage).read_text(encoding="utf-8")
    except ArtifactValidationError:
        raise
    except (OSError, UnicodeError) as error:
        raise ArtifactValidationError(stage, "utf8-read-failed", path) from error


def _read_json(path: Path, stage: str) -> object:
    try:
        return cast(
            object,
            json.loads(
                _read_text(path, stage),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_constant,
            ),
        )
    except ArtifactValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ArtifactValidationError(stage, "json-invalid", path) from error


def _sha256(path: Path, stage: str) -> str:
    _ = _regular(path, stage)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ArtifactValidationError(stage, "hash-read-failed", path) from error
    return digest.hexdigest()


def _topic_path(docs_dir: Path, topic: str) -> Path:
    if _TOPIC.fullmatch(topic) is None:
        raise ArtifactValidationError("paper-index", "topic-invalid")
    root = docs_dir.resolve()
    topic_dir = (root / topic).resolve()
    if topic_dir.parent != root:
        raise ArtifactValidationError("paper-index", "topic-path-invalid")
    return topic_dir


def _selected_papers(
    topic: str,
    docs_dir: Path,
) -> tuple[Path, list[dict[str, object]]]:
    index_path = docs_dir / "papers" / "_papers_index.json"
    value = _read_json(index_path, "paper-index")
    if not isinstance(value, list):
        raise ArtifactValidationError(
            "paper-index",
            "paper-index-list-required",
            index_path,
        )
    selected: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            continue
        row = cast(dict[str, object], raw)
        topics = row.get("topics")
        classifications = row.get("classifications")
        included = (
            isinstance(topics, list)
            and topic in cast(list[object], topics)
        ) or (
            isinstance(classifications, dict)
            and topic in cast(dict[str, object], classifications)
        )
        if not included:
            continue
        slug = row.get("slug")
        if (
            not isinstance(slug, str)
            or _SLUG.fullmatch(slug) is None
            or slug in seen
        ):
            raise ArtifactValidationError(
                "paper-index",
                "selected-slug-invalid",
                index_path,
            )
        seen.add(slug)
        selected.append(row)
    if not selected:
        raise ArtifactValidationError(
            "paper-index",
            "selected-paper-set-empty",
            index_path,
        )
    selected.sort(key=lambda row: cast(str, row["slug"]))
    for row in selected:
        slug = cast(str, row["slug"])
        source = docs_dir / "papers" / slug / "text.md"
        expected = row.get("text_md_sha256")
        if (
            not isinstance(expected, str)
            or _SHA256.fullmatch(expected) is None
            or _sha256(source, "paper-index") != expected
        ):
            raise ArtifactValidationError(
                "paper-index",
                "source-hash-invalid",
                source,
            )
    return index_path, selected


def _validate_reviews(docs_dir: Path, rows: list[dict[str, object]]) -> list[Path]:
    paths: list[Path] = []
    required = (
        "## Essence",
        "## Motivation",
        "## Achievement",
        "## How",
        "## Originality",
        "## Limitation",
        "## Evaluation",
    )
    for row in rows:
        path = docs_dir / "papers" / cast(str, row["slug"]) / "review.md"
        value = _read_text(path, "review")
        if (
            not value.startswith("---\n")
            or "schema_version: v1" not in value.split("---", 2)[1]
            or any(heading not in value for heading in required)
        ):
            raise ArtifactValidationError("review", "review-v1-invalid", path)
        paths.append(path)
    return paths


def _validate_geometry(
    docs_dir: Path,
    rows: list[dict[str, object]],
) -> list[Path]:
    paths: list[Path] = []
    for paper in rows:
        manifest = (
            docs_dir
            / "papers"
            / cast(str, paper["slug"])
            / "figures"
            / "manifest-v1.json"
        )
        value = _read_json(manifest, "geometry")
        if not isinstance(value, dict):
            raise ArtifactValidationError(
                "geometry",
                "geometry-object-required",
                manifest,
            )
        payload = cast(dict[str, object], value)
        source_hash = payload.get("source_pdf_sha256")
        figure_rows = payload.get("rows")
        if (
            payload.get("schema") != "geometry-figures-v1"
            or not isinstance(source_hash, str)
            or _SHA256.fullmatch(source_hash) is None
            or not isinstance(figure_rows, list)
        ):
            raise ArtifactValidationError(
                "geometry",
                "geometry-schema-invalid",
                manifest,
            )
        seen: set[str] = set()
        for raw in cast(list[object], figure_rows):
            if not isinstance(raw, dict):
                raise ArtifactValidationError(
                    "geometry",
                    "geometry-row-invalid",
                    manifest,
                )
            row = cast(dict[str, object], raw)
            relative = row.get("path")
            expected = row.get("sha256")
            if (
                not isinstance(relative, str)
                or re.fullmatch(r"figures/fig[0-9]+\.png", relative) is None
                or relative in seen
                or not isinstance(expected, str)
                or _SHA256.fullmatch(expected) is None
            ):
                raise ArtifactValidationError(
                    "geometry",
                    "geometry-row-invalid",
                    manifest,
                )
            seen.add(relative)
            image = manifest.parent.parent / Path(relative)
            if _sha256(image, "geometry") != expected:
                raise ArtifactValidationError(
                    "geometry",
                    "geometry-hash-invalid",
                    image,
                )
        paths.append(manifest)
    return paths


def _validate_classification(
    path: Path,
    selected_slugs: set[str],
) -> None:
    value = _read_json(path, "classification")
    if not isinstance(value, dict):
        raise ArtifactValidationError(
            "classification",
            "classification-object-required",
            path,
        )
    payload = cast(dict[str, object], value)
    assignments = payload.get("assignments")
    categories = payload.get("categories")
    if (
        not isinstance(assignments, dict)
        or not isinstance(categories, list)
        or not categories
        or not selected_slugs.issubset(cast(dict[str, object], assignments))
    ):
        raise ArtifactValidationError(
            "classification",
            "classification-schema-invalid",
            path,
        )


def _validate_summary(path: Path) -> None:
    value = _read_json(path, "summary")
    if not isinstance(value, list) or not value:
        raise ArtifactValidationError(
            "summary",
            "summary-list-required",
            path,
        )
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            raise ArtifactValidationError("summary", "summary-row-invalid", path)
        row = cast(dict[str, object], raw)
        if (
            not isinstance(row.get("category"), str)
            or not isinstance(row.get("description_ko"), str)
            or not isinstance(row.get("papers"), list)
            or not isinstance(row.get("sub_themes"), list)
        ):
            raise ArtifactValidationError("summary", "summary-row-invalid", path)


def _validate_connections(path: Path, selected_slugs: set[str]) -> None:
    value = _read_json(path, "connection")
    if not isinstance(value, dict):
        raise ArtifactValidationError(
            "connection",
            "connection-object-required",
            path,
        )
    payload = cast(dict[str, object], value)
    if not selected_slugs.issubset(payload):
        raise ArtifactValidationError(
            "connection",
            "connection-coverage-invalid",
            path,
        )
    for slug in selected_slugs:
        if not isinstance(payload[slug], list):
            raise ArtifactValidationError(
                "connection",
                "connection-list-required",
                path,
            )


def _validate_timeline(topic_dir: Path) -> list[Path]:
    category_path = topic_dir / "_category_narratives.json"
    timeline_path = topic_dir / "_timeline_narrative.json"
    categories = _read_json(category_path, "timeline")
    timeline = _read_json(timeline_path, "timeline")
    if not isinstance(categories, list) or not categories:
        raise ArtifactValidationError(
            "timeline",
            "category-narratives-invalid",
            category_path,
        )
    if (
        not isinstance(timeline, dict)
        or not isinstance(
            cast(dict[str, object], timeline).get("executive_summary_ko"),
            str,
        )
        or not isinstance(
            cast(dict[str, object], timeline).get("category_analyses"),
            dict,
        )
    ):
        raise ArtifactValidationError(
            "timeline",
            "timeline-narrative-invalid",
            timeline_path,
        )
    return [category_path, timeline_path]


def _rows(
    stage: str,
    paths: list[Path],
    docs_dir: Path,
) -> dict[str, object]:
    return {
        "artifacts": [
            {
                "path": path.relative_to(docs_dir).as_posix(),
                "sha256": _sha256(path, stage),
            }
            for path in sorted(paths)
        ],
        "stage": stage,
        "status": "valid",
    }


def validate_default_artifacts(
    topic: str,
    docs_dir: str | Path,
) -> list[dict[str, object]]:
    """Validate every default local artifact before reporting success."""
    docs = Path(docs_dir).resolve()
    if docs.is_symlink() or not docs.is_dir():
        raise ArtifactValidationError("paper-index", "docs-directory-required")
    topic_dir = _topic_path(docs, topic)
    index_path, selected = _selected_papers(topic, docs)
    slugs = {cast(str, row["slug"]) for row in selected}
    result: list[dict[str, object]] = []
    result.append(_rows("review", _validate_reviews(docs, selected), docs))
    result.append(_rows("geometry", _validate_geometry(docs, selected), docs))
    result.append(_rows("paper-index", [index_path], docs))

    classification = topic_dir / "_new_classification.json"
    _validate_classification(classification, slugs)
    result.append(_rows("classification", [classification], docs))

    summary = topic_dir / "_category_summaries.json"
    _validate_summary(summary)
    result.append(_rows("summary", [summary], docs))

    connections = topic_dir / "_paper_connections.json"
    _validate_connections(connections, slugs)
    result.append(_rows("connection", [connections], docs))
    result.append(_rows("timeline", _validate_timeline(topic_dir), docs))

    html_paths = [
        docs / "papers" / cast(str, row["slug"]) / "index.html"
        for row in selected
    ]
    for path in html_paths:
        value = _read_text(path, "html").lower()
        if "<html" not in value and "<!doctype html" not in value:
            raise ArtifactValidationError("html", "html-document-invalid", path)
    result.append(_rows("html", html_paths, docs))

    search = topic_dir / "_search_index.json"
    search_value = _read_json(search, "bm25")
    if not isinstance(search_value, dict):
        raise ArtifactValidationError("bm25", "sparse-object-required", search)
    try:
        _ = validate_sparse_index_payload(
            cast(dict[str, object], search_value),
            topic,
        )
        expected = sparse_payload(topic, index_path)
    except SparseIndexError as error:
        raise ArtifactValidationError("bm25", str(error), search) from error
    if search_value != expected:
        raise ArtifactValidationError("bm25", "sparse-source-stale", search)
    result.append(_rows("bm25", [search], docs))

    topic_index = topic_dir / "index.html"
    topic_html = _read_text(topic_index, "topic-index").lower()
    if "<html" not in topic_html and "<!doctype html" not in topic_html:
        raise ArtifactValidationError(
            "topic-index",
            "topic-html-invalid",
            topic_index,
        )
    result.append(_rows("topic-index", [topic_index], docs))

    feed = topic_dir / "feed.xml"
    try:
        root = ET.fromstring(_read_text(feed, "rss"))
    except ET.ParseError as error:
        raise ArtifactValidationError("rss", "rss-xml-invalid", feed) from error
    if root.tag != "rss" or root.find("channel") is None:
        raise ArtifactValidationError("rss", "rss-schema-invalid", feed)
    result.append(_rows("rss", [feed], docs))

    moc_insights = topic_dir / "MOC_Insights.md"
    moc_categories = topic_dir / "MOC_Categories.md"
    if (
        "moc-insights-v1" not in _read_text(moc_insights, "moc")
        or not _read_text(moc_categories, "moc").startswith("#")
    ):
        raise ArtifactValidationError("moc", "moc-schema-invalid", topic_dir)
    result.append(_rows("moc", [moc_insights, moc_categories], docs))

    if tuple(cast(str, row["stage"]) for row in result) != (
        DEFAULT_VALIDATOR_STAGES
    ):
        raise ArtifactValidationError("paper-index", "validator-order-invalid")
    return result


def build_dry_run_plan(
    *,
    entrypoint: str,
    topic: str,
    mode: str = "curate",
    source: str = "zotero",
    images: str = "skip",
    concurrency: int = 1,
    policy_mode: str = "codex",
) -> dict[str, object]:
    empty_merkle = hashlib.sha256(b"[]").hexdigest()
    return {
        "children": [],
        "defaults": {
            "concurrency": concurrency,
            "deploy": False,
            "images": images,
            "mode": mode,
            "source": source,
        },
        "deploy_attempts": [],
        "egress": [],
        "entrypoint": entrypoint,
        "forbidden_counters": FORBIDDEN_COUNTERS,
        "merkle_after": empty_merkle,
        "merkle_before": empty_merkle,
        "normalized_argv": [
            "--concurrency",
            str(concurrency),
            "--dry-run",
            "--images",
            images,
            "--llm-mode",
            policy_mode,
            "--mode",
            mode,
            "--source",
            source,
            "--topic",
            topic,
        ],
        "policy": {
            "allow_paid_api": False,
            "llm_mode": policy_mode,
            "schema_version": 2,
        },
        "policy_mode": policy_mode,
        "read_allowlist": [
            "argv",
            "non-secret-policy-fields",
            "topic-path-metadata",
            "trusted-sidecar-hashes",
        ],
        "read_set": [],
        "schema": "dry-run-plan-v1",
        "schema_version": 1,
        "stages": [
            "review",
            "geometry",
            "paper-index",
            "classification",
            "summary",
            "connection",
            "timeline",
            "html",
            "bm25",
            "topic-index",
            "rss",
            "moc",
        ],
        "topic": topic,
        "validators": list(DEFAULT_VALIDATOR_STAGES),
        "writes": [],
    }


def _existing_hash(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_policy_denied_result(
    topic: str,
    docs_dir: str | Path,
) -> dict[str, object]:
    docs = Path(docs_dir).resolve()
    topic_dir = docs / topic
    candidates: list[Path] = [
        topic_dir / "_new_classification.json",
        topic_dir / "_category_summaries.json",
        topic_dir / "_paper_connections.json",
        topic_dir / "_category_narratives.json",
        topic_dir / "_timeline_narrative.json",
        topic_dir / "MOC_Insights.md",
    ]
    if docs.is_dir():
        candidates.extend(docs.glob("papers/*/review.md"))
        candidates.extend(
            docs.glob("papers/*/figures/manifest-v1.json")
        )
    preserved = {
        path.relative_to(docs).as_posix(): digest
        for path in sorted(set(candidates))
        if (digest := _existing_hash(path)) is not None
    }
    return {
        "completed_deterministic_stages": [],
        "denied_generation_stages": list(DENIED_GENERATION_STAGES),
        "policy_mode": "off",
        "policy": {
            "allow_paid_api": False,
            "llm_mode": "off",
            "schema_version": 2,
        },
        "preserved_artifact_hashes": preserved,
        "schema": "run-result-v1",
        "schema_version": 1,
        "status": "policy_denied",
        "topic": topic,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--entrypoint", required=True)
    _ = parser.add_argument("--topic", default="qa_fixture")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    entrypoint = cast(str, args.entrypoint)
    topic = cast(str, args.topic)
    emit(
        build_dry_run_plan(
            entrypoint=entrypoint,
            topic=topic,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
