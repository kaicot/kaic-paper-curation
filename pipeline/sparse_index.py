"""Deterministic sparse-index-v2 build and transactional legacy migration."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Final, Protocol, cast


SPARSE_SCHEMA: Final = "paper-curation-sparse-index-v2"
JOURNAL_SCHEMA: Final = "search-schema-v1-quarantine-journal"
MOVEFILE_REPLACE_EXISTING: Final = 0x1
MOVEFILE_WRITE_THROUGH: Final = 0x8
FAILPOINTS: Final = (
    "after_prepared",
    "after_legacy_move:1",
    "after_legacy_move:2",
    "after_temp_fsync",
    "after_activation_intent",
    "after_old_backup",
    "after_replace",
    "before_commit",
)
LEGACY_ORDER: Final = (
    "_search_index_emb.bin",
    "_embedding_cache.json",
    "_search_index.bm25-v2.json",
)
ACTIVE_NAME: Final = "_search_index.json"
BM25_CONTRACT: Final = {
    "b": 0.75,
    "idf": "ln(1+(N-df+0.5)/(df+0.5))",
    "k1": 1.5,
    "query_term_frequency": False,
    "tie_break": "document_id-ascending",
}
TOKENIZER_CONTRACT: Final = {
    "ascii_pattern": "[a-z0-9]+",
    "hangul_pattern": "[\\uAC00-\\uD7AF\\u1100-\\u11FF\\u3130-\\u318F]+",
    "id": "ascii-alnum-hangul-bigram-v1",
    "lowercase": "unicode-lower",
    "unicode_normalization": "NFC",
}
TERMINAL_PHASES: Final = frozenset(
    {"committed", "rolled_back", "restored", "purged"}
)


class SparseIndexError(RuntimeError):
    pass


class DurabilityError(SparseIndexError):
    pass


class DurableIO(Protocol):
    def preflight(self, topic_dir: Path, paths: list[Path]) -> None: ...

    def durable_write(self, path: Path, data: bytes) -> None: ...

    def move(self, source: Path, target: Path) -> None: ...

    def sync_file(self, path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class BuildResult:
    active_path: Path
    manifest_path: Path | None
    phase: str
    reused: bool = False


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


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
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SparseIndexError(f"duplicate-json-key:{key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise SparseIndexError(f"invalid-json-constant:{value}")


def _load_json(path: Path) -> object:
    try:
        return cast(
            object,
            json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_constant,
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SparseIndexError(f"invalid-json:{path.name}:{error}") from error


def _timestamp(value: str | None = None) -> str:
    if value is not None:
        return value
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe_topic(topic: str) -> str:
    if topic == "_cross":
        return topic
    if (
        not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", topic)
        or topic in {".", ".."}
        or ".." in topic
    ):
        raise SparseIndexError("invalid-topic")
    return topic


def _contained(root: Path, path: Path) -> Path:
    root_resolved = root.resolve()
    resolved = path.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise SparseIndexError(f"path-outside-topic:{path}")
    current = root_resolved
    try:
        parts = path.absolute().relative_to(root.absolute()).parts
    except ValueError as error:
        raise SparseIndexError(f"path-outside-topic:{path}") from error
    for part in parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise SparseIndexError(f"symlink-refused:{current}")
    return resolved


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SparseIndexError(f"regular-file-required:{path}")


class WindowsDurability:
    """NTFS same-volume durable publication through MoveFileExW."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise DurabilityError("windows-write-through-unavailable")
        self._kernel32: ctypes.WinDLL = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        )
        try:
            self._move_file_ex: object = cast(
                object,
                self._kernel32.MoveFileExW,
            )
        except AttributeError as error:
            raise DurabilityError(
                "write-through-primitive-unavailable"
            ) from error

    @staticmethod
    def _existing_anchor(path: Path) -> Path:
        current = path.resolve(strict=False)
        while not current.exists():
            parent = current.parent
            if parent == current:
                raise DurabilityError(f"no-existing-volume-anchor:{path}")
            current = parent
        return current

    def _volume(self, path: Path) -> tuple[str, str, int]:
        anchor = self._existing_anchor(path)
        volume = ctypes.create_unicode_buffer(261)
        if not self._kernel32.GetVolumePathNameW(
            str(anchor),
            volume,
            len(volume),
        ):
            raise DurabilityError(
                f"volume-path-failed:{ctypes.get_last_error()}"
            )
        filesystem = ctypes.create_unicode_buffer(261)
        if not self._kernel32.GetVolumeInformationW(
            cast(str, cast(object, volume.value)),
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            raise DurabilityError(
                f"volume-info-failed:{ctypes.get_last_error()}"
            )
        volume_name = cast(str, cast(object, volume.value))
        filesystem_name = cast(str, cast(object, filesystem.value))
        return volume_name.casefold(), filesystem_name.upper(), anchor.stat().st_dev

    def preflight(self, topic_dir: Path, paths: list[Path]) -> None:
        if not callable(self._move_file_ex):
            raise DurabilityError("write-through-primitive-unavailable")
        _regular_file(topic_dir) if topic_dir.is_file() else None
        if topic_dir.is_symlink() or not topic_dir.is_dir():
            raise DurabilityError("topic-directory-required")
        baseline_volume, filesystem, baseline_device = self._volume(topic_dir)
        if filesystem != "NTFS":
            raise DurabilityError(f"ntfs-required:{filesystem}")
        for path in paths:
            volume, candidate_filesystem, device = self._volume(path)
            if candidate_filesystem != "NTFS":
                raise DurabilityError(
                    f"ntfs-required:{candidate_filesystem}"
                )
            if volume != baseline_volume or device != baseline_device:
                raise DurabilityError("same-volume-required")

    def move(self, source: Path, target: Path) -> None:
        _regular_file(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        move_file_ex = cast(
            Callable[[str, str, int], int],
            self._move_file_ex,
        )
        if not move_file_ex(str(source), str(target), flags):
            raise DurabilityError(
                f"movefileex-failed:{ctypes.get_last_error()}"
            )

    def sync_file(self, path: Path) -> None:
        _regular_file(path)
        with path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    def durable_write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                _ = handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self.move(temporary, path)
            self.sync_file(path)
        finally:
            if temporary.exists():
                temporary.unlink()


SECTION_ORDER: Final = (
    "Essence",
    "Motivation",
    "Achievement",
    "How",
    "Originality",
    "Limitation",
    "Evaluation",
)


def _clean_markdown(value: str, *, evaluation: bool = False) -> str:
    value = re.sub(r"!\[[^\]]*]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", value)
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|"):
            continue
        if evaluation and re.match(
            r"^-\s*(?:Novelty|Technical Soundness|Significance|Clarity|Overall):",
            line,
            re.IGNORECASE,
        ):
            continue
        line = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", line)
        line = re.sub(r"[`*_>#]+", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _review_sections(text: str) -> list[dict[str, str]]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    found: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        normalized = (
            "Limitation"
            if heading in {"Limitation", "Limitation & Further Study"}
            else heading
        )
        if normalized not in SECTION_ORDER:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found[normalized] = _clean_markdown(
            text[match.end():end],
            evaluation=normalized == "Evaluation",
        )
    return [
        {"name": name, "text": found.get(name, "")}
        for name in SECTION_ORDER
        if found.get(name, "")
    ]


def _document_source(
    topic: str,
    papers_index: Path,
) -> list[dict[str, object]]:
    _regular_file(papers_index)
    raw = _load_json(papers_index)
    if not isinstance(raw, list):
        raise SparseIndexError("paper-index-must-be-list")
    selected_rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            continue
        paper = cast(dict[str, object], item)
        topics = paper.get("topics")
        classifications = paper.get("classifications")
        included = (
            isinstance(topics, list)
            and topic in cast(list[object], topics)
        ) or (
            isinstance(classifications, dict)
            and topic in cast(dict[str, object], classifications)
        )
        if not included:
            continue
        slug = str(paper.get("slug", "")).strip()
        if not slug:
            raise SparseIndexError("selected-paper-missing-slug")
        if slug in seen:
            raise SparseIndexError(f"duplicate-slug:{slug}")
        seen.add(slug)
        selected_rows.append((slug, str(paper.get("title", ""))))
    selected: list[dict[str, object]] = []
    for slug, title in sorted(selected_rows):
        review_path = papers_index.parent / slug / "review.md"
        _regular_file(review_path)
        raw = review_path.read_bytes()
        try:
            review = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SparseIndexError(f"review-utf8-required:{slug}") from error
        sections = _review_sections(review)
        content = {"sections": sections, "title": title}
        selected.append(
            {
                "content": content,
                "content_sha256": _sha256_bytes(_canonical_json(content)),
                "review_path": f"papers/{slug}/review.md",
                "review_sha256": _sha256_bytes(raw),
                "slug": slug,
                "title": title,
            }
        )
    return selected


def tokenize(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFC", value).lower()
    tokens: list[str] = []
    pattern = r"[a-z0-9]+|[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]+"
    for match in re.finditer(pattern, normalized):
        token = match.group(0)
        if re.fullmatch(r"[a-z0-9]+", token):
            tokens.append(token)
        elif len(token) == 1:
            tokens.append(token)
        else:
            tokens.extend(
                token[index:index + 2]
                for index in range(len(token) - 1)
            )
    return tokens


def validate_sparse_index_payload(
    value: dict[str, object],
    topic: str,
) -> tuple[list[dict[str, object]], dict[str, list[list[int]]]]:
    """Validate the complete machine-consumed sparse v2 structure."""
    cross = topic == "_cross"
    required_root: set[str] = {
        "average_document_length",
        "bm25",
        "document_count",
        "documents",
        "postings",
        "schema",
        "schema_version",
        "source",
        "source_file_count",
        "source_fingerprint",
        "tokenizer",
        "topic",
        "total_document_length",
    } | ({"topics"} if cross else set())
    if set(value) != required_root:
        raise SparseIndexError("sparse-root-fields-invalid")
    if (
        value.get("schema") != SPARSE_SCHEMA
        or value.get("schema_version") != 2
        or value.get("topic") != topic
        or value.get("bm25") != BM25_CONTRACT
        or value.get("tokenizer") != TOKENIZER_CONTRACT
    ):
        raise SparseIndexError("sparse-contract-invalid")
    raw_documents = value.get("documents")
    raw_postings = value.get("postings")
    if not isinstance(raw_documents, list) or not isinstance(raw_postings, dict):
        raise SparseIndexError("sparse-containers-invalid")
    hash_pattern = re.compile(r"[0-9a-f]{64}")
    documents: list[dict[str, object]] = []
    prior_slug = ""
    for document_id, raw_document in enumerate(cast(list[object], raw_documents)):
        if not isinstance(raw_document, dict):
            raise SparseIndexError("sparse-document-invalid")
        document = cast(dict[str, object], raw_document)
        required_document: set[str] = {
            "content_sha256",
            "id",
            "length",
            "slug",
            "source_sha256",
            "title",
        } | ({"topics"} if cross else set())
        if set(document) != required_document:
            raise SparseIndexError("sparse-document-fields-invalid")
        slug = document.get("slug")
        length = document.get("length")
        content_sha = document.get("content_sha256")
        source_sha = document.get("source_sha256")
        if (
            document.get("id") != document_id
            or not isinstance(slug, str)
            or not slug
            or slug <= prior_slug
            or not isinstance(document.get("title"), str)
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
            or not isinstance(content_sha, str)
            or hash_pattern.fullmatch(content_sha) is None
            or not isinstance(source_sha, str)
            or hash_pattern.fullmatch(source_sha) is None
        ):
            raise SparseIndexError("sparse-document-metadata-invalid")
        if cross:
            topics = document.get("topics")
            topic_items = (
                cast(list[object], topics)
                if isinstance(topics, list)
                else []
            )
            if (
                not isinstance(topics, list)
                or not topics
                or not all(
                    isinstance(item, str) and item and item != "_cross"
                    for item in topic_items
                )
            ):
                raise SparseIndexError("sparse-cross-topics-invalid")
            normalized_topics = cast(list[str], topic_items)
            if normalized_topics != sorted(set(normalized_topics)):
                raise SparseIndexError("sparse-cross-topics-invalid")
        prior_slug = slug
        documents.append(document)
    if value.get("document_count") != len(documents):
        raise SparseIndexError("sparse-document-count-mismatch")
    total_length = sum(cast(int, item["length"]) for item in documents)
    if value.get("total_document_length") != total_length:
        raise SparseIndexError("sparse-total-length-mismatch")
    average = value.get("average_document_length")
    expected_average = total_length / len(documents) if documents else 0.0
    if (
        not isinstance(average, (int, float))
        or isinstance(average, bool)
        or float(average) != expected_average
    ):
        raise SparseIndexError("sparse-average-length-mismatch")
    terms = list(cast(dict[str, object], raw_postings))
    if terms != sorted(terms):
        raise SparseIndexError("sparse-terms-not-sorted")
    totals = [0] * len(documents)
    postings: dict[str, list[list[int]]] = {}
    for term, raw_rows in cast(dict[str, object], raw_postings).items():
        if (
            not term
            or tokenize(term) != [term]
            or not isinstance(raw_rows, list)
        ):
            raise SparseIndexError("sparse-posting-term-invalid")
        prior_document_id = -1
        rows: list[list[int]] = []
        for raw_row in cast(list[object], raw_rows):
            if (
                not isinstance(raw_row, list)
                or len(cast(list[object], raw_row)) != 2
                or not all(
                    isinstance(item, int) and not isinstance(item, bool)
                    for item in cast(list[object], raw_row)
                )
            ):
                raise SparseIndexError("sparse-posting-row-invalid")
            document_id, frequency = cast(list[int], raw_row)
            if (
                document_id <= prior_document_id
                or document_id < 0
                or document_id >= len(documents)
                or frequency <= 0
            ):
                raise SparseIndexError("sparse-posting-range-invalid")
            prior_document_id = document_id
            totals[document_id] += frequency
            rows.append([document_id, frequency])
        postings[term] = rows
    if totals != [cast(int, item["length"]) for item in documents]:
        raise SparseIndexError("sparse-posting-total-mismatch")
    fingerprint = value.get("source_fingerprint")
    source_file_count = value.get("source_file_count")
    if (
        not isinstance(fingerprint, str)
        or hash_pattern.fullmatch(fingerprint) is None
        or not isinstance(source_file_count, int)
        or isinstance(source_file_count, bool)
        or source_file_count < 1
    ):
        raise SparseIndexError("sparse-source-metadata-invalid")
    return documents, postings


def sparse_payload(topic: str, papers_index: Path) -> dict[str, object]:
    source = _document_source(topic, papers_index)
    selected_records = [
        {"slug": str(paper["slug"]), "title": str(paper["title"])}
        for paper in source
    ]
    source_manifest: dict[str, object] = {
        "index": {
            "path": "papers/_papers_index.json",
            "selected_records_sha256": _sha256_bytes(
                _canonical_json(selected_records)
            ),
        },
        "reviews": [
            {
                "path": str(paper["review_path"]),
                "sha256": str(paper["review_sha256"]),
            }
            for paper in source
        ],
    }
    source_fingerprint = _sha256_bytes(
        _canonical_json({"source": source_manifest, "topic": topic})
    )
    documents: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    for document_id, paper in enumerate(source):
        content = cast(dict[str, object], paper["content"])
        sections = cast(list[dict[str, str]], content["sections"])
        text = "\n".join(
            [
                str(paper["title"]),
                *(section["text"] for section in sections),
            ]
        )
        terms = tokenize(text)
        frequencies: dict[str, int] = {}
        for term in terms:
            frequencies[term] = frequencies.get(term, 0) + 1
        documents.append(
            {
                "content_sha256": str(paper["content_sha256"]),
                "id": document_id,
                "length": len(terms),
                "slug": str(paper["slug"]),
                "source_sha256": str(paper["review_sha256"]),
                "title": str(paper["title"]),
            }
        )
        for term in sorted(frequencies):
            postings.setdefault(term, []).append(
                [document_id, frequencies[term]]
            )
    total_length = sum(
        cast(int, document["length"])
        for document in documents
    )
    return {
        "bm25": BM25_CONTRACT,
        "average_document_length": (
            total_length / len(documents) if documents else 0.0
        ),
        "document_count": len(documents),
        "documents": documents,
        "postings": {
            term: postings[term]
            for term in sorted(postings)
        },
        "schema": SPARSE_SCHEMA,
        "schema_version": 2,
        "source": source_manifest,
        "source_file_count": len(source) + 1,
        "source_fingerprint": source_fingerprint,
        "tokenizer": TOKENIZER_CONTRACT,
        "total_document_length": total_length,
        "topic": topic,
    }


def current_source_sha256(topic: str, docs_dir: Path) -> tuple[str, int]:
    payload = sparse_payload(
        _safe_topic(topic),
        docs_dir / "papers" / "_papers_index.json",
    )
    return str(payload["source_fingerprint"]), cast(int, payload["document_count"])


def cross_sparse_payload(
    topics: list[str],
    docs_dir: Path,
) -> dict[str, object]:
    selected_topics = sorted({_safe_topic(topic) for topic in topics})
    if not selected_topics:
        raise SparseIndexError("cross-topics-required")
    if "_cross" in selected_topics:
        raise SparseIndexError("cross-self-source-refused")
    docs_root = docs_dir.resolve()
    documents_by_slug: dict[str, dict[str, object]] = {}
    frequencies_by_slug: dict[str, dict[str, int]] = {}
    source_indexes: dict[str, dict[str, object]] = {}
    bm25_contract: object | None = None
    tokenizer_contract: object | None = None
    for topic in selected_topics:
        path = docs_root / topic / ACTIVE_NAME
        _regular_file(path)
        raw_bytes = path.read_bytes()
        value = _load_json(path)
        if not _is_sparse_v2(value, topic):
            raise SparseIndexError(f"sparse-source-required:{topic}")
        index = cast(dict[str, object], value)
        if raw_bytes != _canonical_json(index):
            raise SparseIndexError(f"sparse-source-noncanonical:{topic}")
        _ = validate_sparse_index_payload(index, topic)
        expected_index = sparse_payload(
            topic,
            docs_root / "papers" / "_papers_index.json",
        )
        if _canonical_json(index) != _canonical_json(expected_index):
            raise SparseIndexError(f"sparse-source-stale:{topic}")
        if bm25_contract is None:
            bm25_contract = index.get("bm25")
            tokenizer_contract = index.get("tokenizer")
        elif (
            index.get("bm25") != bm25_contract
            or index.get("tokenizer") != tokenizer_contract
        ):
            raise SparseIndexError("cross-index-contract-drift")
        raw_documents = index.get("documents")
        raw_postings = index.get("postings")
        if not isinstance(raw_documents, list) or not isinstance(raw_postings, dict):
            raise SparseIndexError("sparse-source-shape-invalid")
        source_documents = [
            cast(dict[str, object], document)
            for document in cast(list[object], raw_documents)
            if isinstance(document, dict)
        ]
        if len(source_documents) != len(cast(list[object], raw_documents)):
            raise SparseIndexError("sparse-source-document-invalid")
        source_indexes[topic] = {
            "index_sha256": _sha256_bytes(raw_bytes),
            "path": f"{topic}/{ACTIVE_NAME}",
            "source_fingerprint": str(index.get("source_fingerprint", "")),
        }
        for document_id, document in enumerate(source_documents):
            if document.get("id") != document_id:
                raise SparseIndexError("sparse-source-document-id-invalid")
            slug = str(document.get("slug", ""))
            if not slug:
                raise SparseIndexError("sparse-source-slug-invalid")
            candidate = {
                key: document[key]
                for key in (
                    "content_sha256",
                    "length",
                    "slug",
                    "source_sha256",
                    "title",
                )
            }
            prior = documents_by_slug.get(slug)
            if prior is not None and any(
                prior[key] != candidate[key]
                for key in candidate
            ):
                raise SparseIndexError(f"cross-duplicate-drift:{slug}")
            if prior is None:
                candidate["topics"] = [topic]
                documents_by_slug[slug] = candidate
                frequencies_by_slug[slug] = {}
            else:
                cast(list[str], prior["topics"]).append(topic)
        for term, raw_rows in cast(dict[str, object], raw_postings).items():
            if not isinstance(raw_rows, list):
                raise SparseIndexError("sparse-source-postings-invalid")
            for raw_row in cast(list[object], raw_rows):
                if (
                    not isinstance(raw_row, list)
                    or len(cast(list[object], raw_row)) != 2
                    or not all(
                        isinstance(item, int)
                        for item in cast(list[object], raw_row)
                    )
                ):
                    raise SparseIndexError("sparse-source-posting-row-invalid")
                document_id, frequency = cast(list[int], raw_row)
                if (
                    document_id < 0
                    or document_id >= len(source_documents)
                    or frequency <= 0
                ):
                    raise SparseIndexError("sparse-source-posting-range-invalid")
                slug = str(source_documents[document_id]["slug"])
                prior_frequency = frequencies_by_slug[slug].get(term)
                if prior_frequency not in (None, frequency):
                    raise SparseIndexError(f"cross-duplicate-tf-drift:{slug}")
                frequencies_by_slug[slug][term] = frequency
    documents: list[dict[str, object]] = []
    postings: dict[str, list[list[int]]] = {}
    for document_id, slug in enumerate(sorted(documents_by_slug)):
        document = documents_by_slug[slug]
        document["id"] = document_id
        document["topics"] = sorted(cast(list[str], document["topics"]))
        documents.append(document)
        for term, frequency in sorted(frequencies_by_slug[slug].items()):
            postings.setdefault(term, []).append([document_id, frequency])
    total_length = sum(cast(int, document["length"]) for document in documents)
    source_fingerprint = _sha256_bytes(
        _canonical_json(
            {
                "source_indexes": source_indexes,
                "topics": selected_topics,
            }
        )
    )
    payload: dict[str, object] = {
        "average_document_length": (
            total_length / len(documents) if documents else 0.0
        ),
        "bm25": bm25_contract,
        "document_count": len(documents),
        "documents": documents,
        "postings": {
            term: postings[term]
            for term in sorted(postings)
        },
        "schema": SPARSE_SCHEMA,
        "schema_version": 2,
        "source": {"indexes": source_indexes},
        "source_file_count": len(selected_topics),
        "source_fingerprint": source_fingerprint,
        "tokenizer": tokenizer_contract,
        "topic": "_cross",
        "topics": selected_topics,
        "total_document_length": total_length,
    }
    return payload


def build_cross_sparse_index(
    topics: list[str],
    docs_dir: Path,
    *,
    durability: DurableIO | None = None,
    run_id: str | None = None,
    timestamp: str | None = None,
) -> BuildResult:
    docs_root = docs_dir.resolve()
    payload = cross_sparse_payload(topics, docs_root)
    return _activate_sparse_payload(
        "_cross",
        docs_root,
        payload,
        durability=durability,
        run_id=run_id,
        timestamp=timestamp,
        create_topic=True,
    )


def _is_sparse_v2(value: object, topic: str) -> bool:
    if not isinstance(value, dict):
        return False
    item = cast(dict[str, object], value)
    return (
        item.get("schema") == SPARSE_SCHEMA
        and item.get("schema_version") == 2
        and item.get("topic") == topic
        and isinstance(item.get("documents"), list)
        and isinstance(item.get("postings"), dict)
        and isinstance(item.get("source"), dict)
    )


def _legacy_active(path: Path, topic: str) -> tuple[str, str | None]:
    value = _load_json(path)
    if _is_sparse_v2(value, topic):
        return "prior-sparse-v2", None
    if not isinstance(value, dict):
        raise SparseIndexError("legacy-active-shape-invalid")
    item = cast(dict[str, object], value)
    if (
        not isinstance(item.get("papers"), dict)
        or not isinstance(item.get("chunks"), list)
        or not isinstance(item.get("model"), str)
        or not isinstance(item.get("dim"), int)
        or item.get("quant") != "int8-l2norm"
    ):
        raise SparseIndexError("legacy-active-provenance-invalid")
    sidecar = item.get("emb_file")
    if sidecar not in (None, "_search_index_emb.bin"):
        raise SparseIndexError("legacy-sidecar-name-refused")
    return "legacy-dense-active", cast(str | None, sidecar)


def _artifact_row(
    topic_dir: Path,
    run_root: Path,
    path: Path,
    reason: str,
    timestamp: str,
) -> dict[str, object]:
    _regular_file(path)
    _ = _contained(topic_dir, path)
    quarantine = run_root / "legacy" / path.name
    _ = _contained(topic_dir, quarantine)
    return {
        "original_path": path.relative_to(topic_dir).as_posix(),
        "quarantine_path": quarantine.relative_to(topic_dir).as_posix(),
        "reason": reason,
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
        "timestamp": timestamp,
    }


def _discover_artifacts(
    topic_dir: Path,
    run_root: Path,
    topic: str,
    timestamp: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    active = topic_dir / ACTIVE_NAME
    sidecar_reference: str | None = None
    active_reason: str | None = None
    if active.exists():
        _regular_file(active)
        active_reason, sidecar_reference = _legacy_active(active, topic)
        rows.append(
            _artifact_row(
                topic_dir,
                run_root,
                active,
                active_reason,
                timestamp,
            )
        )
    dense_sidecar = topic_dir / "_search_index_emb.bin"
    if dense_sidecar.exists():
        if sidecar_reference != dense_sidecar.name:
            raise SparseIndexError("unbound-dense-sidecar-refused")
        rows.append(
            _artifact_row(
                topic_dir,
                run_root,
                dense_sidecar,
                "legacy-dense-sidecar",
                timestamp,
            )
        )
    embedding_cache = topic_dir / "_embedding_cache.json"
    if embedding_cache.exists():
        if active_reason != "legacy-dense-active":
            raise SparseIndexError("unbound-embedding-cache-refused")
        if not isinstance(_load_json(embedding_cache), dict):
            raise SparseIndexError("embedding-cache-shape-invalid")
        rows.append(
            _artifact_row(
                topic_dir,
                run_root,
                embedding_cache,
                "legacy-resume-cache",
                timestamp,
            )
        )
    transitional = topic_dir / "_search_index.bm25-v2.json"
    if transitional.exists():
        if not _is_sparse_v2(_load_json(transitional), topic):
            raise SparseIndexError("transitional-sidecar-provenance-invalid")
        rows.append(
            _artifact_row(
                topic_dir,
                run_root,
                transitional,
                "transitional-sparse-sidecar",
                timestamp,
            )
        )
    order = {name: index for index, name in enumerate((ACTIVE_NAME, *LEGACY_ORDER))}
    return sorted(
        rows,
        key=lambda row: order[Path(str(row["original_path"])).name],
    )


def _manifest_paths(
    topic_dir: Path,
    manifest: dict[str, object],
) -> tuple[Path, Path, Path]:
    run_id = str(manifest.get("run_id", ""))
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z-]*", run_id):
        raise SparseIndexError("invalid-run-id")
    run_root = (
        topic_dir
        / ".curation-quarantine"
        / "search-schema-v1"
        / run_id
    )
    manifest_path = run_root / "manifest.json"
    candidate = run_root / "candidate" / ACTIVE_NAME
    return run_root, manifest_path, candidate


def _history(
    manifest: dict[str, object],
    phase: str,
    timestamp: str | None = None,
) -> None:
    moment = _timestamp(timestamp)
    raw_history = manifest.setdefault("history", [])
    if not isinstance(raw_history, list):
        raise SparseIndexError("manifest-history-invalid")
    cast(list[object], raw_history).append(
        {"phase": phase, "timestamp": moment}
    )
    manifest["phase"] = phase
    manifest["updated_at"] = moment


def _write_manifest(
    manifest_path: Path,
    manifest: dict[str, object],
    durability: DurableIO,
) -> None:
    durability.durable_write(manifest_path, _canonical_json(manifest))


def _failpoint(name: str) -> None:
    selected = os.environ.get("PAPER_CURATION_FAILPOINT")
    testing = os.environ.get("PAPER_CURATION_TESTING") == "1"
    if selected is None:
        return
    if not testing:
        raise SparseIndexError("failpoint-requires-testing-mode")
    if selected not in FAILPOINTS:
        raise SparseIndexError(f"unknown-failpoint:{selected}")
    if selected == name:
        os._exit(86)


def _validate_failpoint_before_mutation() -> None:
    selected = os.environ.get("PAPER_CURATION_FAILPOINT")
    if selected is None:
        return
    if os.environ.get("PAPER_CURATION_TESTING") != "1":
        raise SparseIndexError("failpoint-requires-testing-mode")
    if selected not in FAILPOINTS:
        raise SparseIndexError(f"unknown-failpoint:{selected}")


def _row_paths(
    topic_dir: Path,
    row: dict[str, object],
) -> tuple[Path, Path]:
    original_text = str(row.get("original_path", ""))
    quarantine_text = str(row.get("quarantine_path", ""))
    if (
        Path(original_text).name not in {ACTIVE_NAME, *LEGACY_ORDER}
        or Path(original_text).parent != Path(".")
    ):
        raise SparseIndexError("manifest-original-not-allowlisted")
    original = topic_dir / original_text
    quarantine = topic_dir / quarantine_text
    _ = _contained(topic_dir, original)
    _ = _contained(topic_dir, quarantine)
    return original, quarantine


def _load_manifest(
    manifest_path: Path,
    docs_dir: Path,
) -> tuple[dict[str, object], Path, Path]:
    if not manifest_path.is_absolute():
        raise SparseIndexError("absolute-manifest-path-required")
    _regular_file(manifest_path)
    raw = _load_json(manifest_path)
    if not isinstance(raw, dict):
        raise SparseIndexError("manifest-shape-invalid")
    manifest = cast(dict[str, object], raw)
    if (
        manifest.get("schema") != JOURNAL_SCHEMA
        or manifest.get("schema_version") != 1
    ):
        raise SparseIndexError("manifest-schema-invalid")
    topic = _safe_topic(str(manifest.get("topic", "")))
    topic_dir = (docs_dir / topic).resolve()
    run_root, expected_manifest, _candidate = _manifest_paths(
        topic_dir,
        manifest,
    )
    if manifest_path.resolve() != expected_manifest.resolve():
        raise SparseIndexError("manifest-path-invalid")
    _ = _contained(topic_dir, run_root)
    raw_rows = manifest.get("artifacts")
    if not isinstance(raw_rows, list):
        raise SparseIndexError("manifest-artifacts-invalid")
    for raw_row in cast(list[object], raw_rows):
        if not isinstance(raw_row, dict):
            raise SparseIndexError("manifest-artifact-row-invalid")
        row = cast(dict[str, object], raw_row)
        original, quarantine = _row_paths(topic_dir, row)
        expected = (
            run_root
            / "legacy"
            / Path(str(row["original_path"])).name
        )
        if quarantine.resolve(strict=False) != expected.resolve(strict=False):
            raise SparseIndexError("manifest-quarantine-path-invalid")
        if not isinstance(row.get("size"), int) or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(row.get("sha256", "")),
        ):
            raise SparseIndexError("manifest-artifact-evidence-invalid")
        _ = original
    return manifest, topic_dir, run_root


def _artifact_states_valid(
    manifest: dict[str, object],
    topic_dir: Path,
) -> None:
    raw_rows = cast(list[object], manifest["artifacts"])
    for raw_row in raw_rows:
        row = cast(dict[str, object], raw_row)
        original, quarantine = _row_paths(topic_dir, row)
        expected_hash = str(row["sha256"])
        expected_size = cast(int, row["size"])
        present = [path for path in (original, quarantine) if path.exists()]
        if not present:
            raise SparseIndexError(f"journaled-artifact-missing:{original.name}")
        for path in present:
            _regular_file(path)
            if (
                path.stat().st_size != expected_size
                or _sha256_file(path) != expected_hash
            ):
                if (
                    original.name == ACTIVE_NAME
                    and path == original
                    and _candidate_matches(manifest, path)
                ):
                    continue
                raise SparseIndexError(f"journaled-artifact-drift:{path}")


def _candidate_matches(
    manifest: dict[str, object],
    path: Path,
) -> bool:
    candidate = manifest.get("candidate")
    return (
        isinstance(candidate, dict)
        and path.is_file()
        and not path.is_symlink()
        and _sha256_file(path)
        == str(cast(dict[str, object], candidate).get("sha256", ""))
    )


def _operation_durability(
    durability: DurableIO | None,
) -> DurableIO:
    return durability if durability is not None else WindowsDurability()


def _pending_recovery(
    topic_dir: Path,
    docs_dir: Path,
    durability: DurableIO,
) -> None:
    journal_root = (
        topic_dir
        / ".curation-quarantine"
        / "search-schema-v1"
    )
    if not journal_root.is_dir():
        return
    for manifest_path in sorted(journal_root.glob("*/manifest.json")):
        raw = _load_json(manifest_path)
        if not isinstance(raw, dict):
            raise SparseIndexError("pending-manifest-invalid")
        phase = str(cast(dict[str, object], raw).get("phase", ""))
        if phase not in TERMINAL_PHASES:
            _ = recover_transaction(
                manifest_path.resolve(),
                docs_dir,
                durability=durability,
            )


def build_sparse_index(
    topic: str,
    docs_dir: Path,
    *,
    durability: DurableIO | None = None,
    run_id: str | None = None,
    timestamp: str | None = None,
) -> BuildResult:
    selected_topic = _safe_topic(topic)
    docs_root = docs_dir.resolve()
    payload = sparse_payload(
        selected_topic,
        docs_root / "papers" / "_papers_index.json",
    )
    return _activate_sparse_payload(
        selected_topic,
        docs_root,
        payload,
        durability=durability,
        run_id=run_id,
        timestamp=timestamp,
    )


def _activate_sparse_payload(
    selected_topic: str,
    docs_root: Path,
    payload: dict[str, object],
    *,
    durability: DurableIO | None = None,
    run_id: str | None = None,
    timestamp: str | None = None,
    create_topic: bool = False,
) -> BuildResult:
    topic_dir = (docs_root / selected_topic).resolve()
    _ = _contained(docs_root, topic_dir)
    topic_exists = topic_dir.is_dir() and not topic_dir.is_symlink()
    if not topic_exists and not create_topic:
        raise SparseIndexError("topic-directory-required")
    if topic_dir.exists() and not topic_exists:
        raise SparseIndexError("topic-directory-required")
    _validate_failpoint_before_mutation()
    durable = _operation_durability(durability)
    if topic_exists:
        _pending_recovery(topic_dir, docs_root, durable)
    candidate_bytes = _canonical_json(payload)
    active = topic_dir / ACTIVE_NAME
    if active.is_file() and not active.is_symlink():
        existing = active.read_bytes()
        if existing == candidate_bytes:
            return BuildResult(active, None, "committed", reused=True)
    transaction_id = run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z-]*", transaction_id):
        raise SparseIndexError("invalid-run-id")
    moment = _timestamp(timestamp)
    run_root = (
        topic_dir
        / ".curation-quarantine"
        / "search-schema-v1"
        / transaction_id
    )
    manifest_path = run_root / "manifest.json"
    candidate_path = run_root / "candidate" / ACTIVE_NAME
    rows = _discover_artifacts(
        topic_dir,
        run_root,
        selected_topic,
        moment,
    )
    paths = [
        manifest_path,
        candidate_path,
        active,
        *(topic_dir / str(row["quarantine_path"]) for row in rows),
    ]
    durable.preflight(topic_dir if topic_exists else docs_root, paths)
    if not topic_exists:
        topic_dir.mkdir()
    run_root.joinpath("legacy").mkdir(parents=True, exist_ok=False)
    run_root.joinpath("candidate").mkdir(parents=True, exist_ok=False)
    restore_command = [
        sys.executable,
        str(Path(__file__).with_name("build_search_index.py")),
        "--restore",
        "--manifest",
        str(manifest_path),
        "--docs-dir",
        str(docs_root),
    ]
    manifest: dict[str, object] = {
        "activation_intent": None,
        "artifacts": rows,
        "candidate": {
            "active_path": ACTIVE_NAME,
            "path": candidate_path.relative_to(topic_dir).as_posix(),
            "sha256": _sha256_bytes(candidate_bytes),
            "size": len(candidate_bytes),
        },
        "created_at": moment,
        "history": [],
        "phase": "prepared",
        "reason": "activate-sparse-index-v2",
        "restore_command": restore_command,
        "run_id": transaction_id,
        "schema": JOURNAL_SCHEMA,
        "schema_version": 1,
        "topic": selected_topic,
        "updated_at": moment,
    }
    _history(manifest, "prepared", moment)
    _write_manifest(manifest_path, manifest, durable)
    _failpoint("after_prepared")
    try:
        non_active_rows = [
            row
            for row in rows
            if str(row["original_path"])
            != ACTIVE_NAME
        ]
        legacy_number = 0
        for row in non_active_rows:
            original, quarantine = _row_paths(topic_dir, row)
            if (
                original.stat().st_size != cast(int, row["size"])
                or _sha256_file(original) != str(row["sha256"])
            ):
                raise SparseIndexError(f"artifact-changed-before-move:{original}")
            durable.move(original, quarantine)
            legacy_number += 1
            phase = f"legacy_moved:{legacy_number}"
            _history(manifest, phase)
            _write_manifest(manifest_path, manifest, durable)
            if legacy_number <= 2:
                _failpoint(f"after_legacy_move:{legacy_number}")
        durable.durable_write(candidate_path, candidate_bytes)
        _history(manifest, "temp_fsynced")
        _write_manifest(manifest_path, manifest, durable)
        _failpoint("after_temp_fsync")
        active_row = next(
            (
                row
                for row in rows
                if str(row["original_path"])
                == ACTIVE_NAME
            ),
            None,
        )
        manifest["activation_intent"] = {
            "final_destination": ACTIVE_NAME,
            "prior_active": (
                {
                    "path": ACTIVE_NAME,
                    "sha256": str(active_row["sha256"]),
                    "size": cast(int, active_row["size"]),
                }
                if active_row is not None
                else {"state": "absent"}
            ),
            "rollback_destination": (
                str(active_row["quarantine_path"])
                if active_row is not None
                else None
            ),
            "v2_temp": {
                "path": candidate_path.relative_to(topic_dir).as_posix(),
                "sha256": _sha256_bytes(candidate_bytes),
                "size": len(candidate_bytes),
            },
        }
        _history(manifest, "activation_intent")
        _write_manifest(manifest_path, manifest, durable)
        _failpoint("after_activation_intent")
        if active_row is not None:
            original, quarantine = _row_paths(topic_dir, active_row)
            if (
                original.stat().st_size != cast(int, active_row["size"])
                or _sha256_file(original) != str(active_row["sha256"])
            ):
                raise SparseIndexError("active-changed-before-backup")
            durable.move(original, quarantine)
        _history(manifest, "old_backed_up")
        _write_manifest(manifest_path, manifest, durable)
        _failpoint("after_old_backup")
        durable.move(candidate_path, active)
        durable.sync_file(active)
        if active.read_bytes() != candidate_bytes:
            raise DurabilityError("activated-v2-hash-mismatch")
        _history(manifest, "replaced")
        _write_manifest(manifest_path, manifest, durable)
        _failpoint("after_replace")
        _failpoint("before_commit")
        _history(manifest, "committed")
        _write_manifest(manifest_path, manifest, durable)
    except BaseException:
        _ = recover_transaction(
            manifest_path.resolve(),
            docs_root,
            durability=durable,
        )
        raise
    return BuildResult(active, manifest_path, "committed")


def _rollback(
    manifest_path: Path,
    docs_dir: Path,
    *,
    durability: DurableIO,
    terminal_phase: str,
    allow_committed: bool,
) -> BuildResult:
    manifest, topic_dir, run_root = _load_manifest(
        manifest_path,
        docs_dir.resolve(),
    )
    phase = str(manifest.get("phase", ""))
    if phase == terminal_phase:
        return BuildResult(
            topic_dir / ACTIVE_NAME,
            manifest_path,
            terminal_phase,
        )
    if phase == "committed" and not allow_committed:
        return BuildResult(
            topic_dir / ACTIVE_NAME,
            manifest_path,
            "committed",
        )
    if phase in TERMINAL_PHASES and not (
        phase == "committed" and allow_committed
    ):
        raise SparseIndexError(f"journal-terminal:{phase}")
    _artifact_states_valid(manifest, topic_dir)
    active = topic_dir / ACTIVE_NAME
    recovered_candidate = run_root / "candidate" / (
        "restored-v2.json"
        if terminal_phase == "restored"
        else "recovered-v2.json"
    )
    destinations = [manifest_path, recovered_candidate]
    rows = [
        cast(dict[str, object], row)
        for row in cast(list[object], manifest["artifacts"])
    ]
    destinations.extend(
        path
        for row in rows
        for path in _row_paths(topic_dir, row)
    )
    durability.preflight(topic_dir, destinations)
    if active.exists() and _candidate_matches(manifest, active):
        durability.move(active, recovered_candidate)
    elif active.exists():
        active_row = next(
            (
                row
                for row in rows
                if str(row["original_path"]) == ACTIVE_NAME
            ),
            None,
        )
        if (
            active_row is None
            or _sha256_file(active) != str(active_row["sha256"])
        ):
            raise SparseIndexError("unknown-active-refused")
    for row in rows:
        original, quarantine = _row_paths(topic_dir, row)
        expected = str(row["sha256"])
        if quarantine.exists():
            if original.exists():
                if _sha256_file(original) == expected:
                    raise SparseIndexError(
                        f"duplicate-journaled-artifact:{original.name}"
                    )
                raise SparseIndexError(f"restore-target-occupied:{original}")
            if _sha256_file(quarantine) != expected:
                raise SparseIndexError(f"quarantine-hash-mismatch:{quarantine}")
            durability.move(quarantine, original)
            durability.sync_file(original)
        elif not original.exists() or _sha256_file(original) != expected:
            raise SparseIndexError(f"restore-source-missing:{original.name}")
    _history(manifest, terminal_phase)
    _write_manifest(manifest_path, manifest, durability)
    return BuildResult(active, manifest_path, terminal_phase)


def recover_transaction(
    manifest_path: Path,
    docs_dir: Path,
    *,
    durability: DurableIO | None = None,
) -> BuildResult:
    return _rollback(
        manifest_path,
        docs_dir,
        durability=_operation_durability(durability),
        terminal_phase="rolled_back",
        allow_committed=False,
    )


def restore_transaction(
    manifest_path: Path,
    docs_dir: Path,
    *,
    durability: DurableIO | None = None,
) -> BuildResult:
    manifest, _topic_dir, _run_root = _load_manifest(
        manifest_path,
        docs_dir.resolve(),
    )
    phase = str(manifest.get("phase", ""))
    if phase == "restored":
        return BuildResult(
            docs_dir.resolve() / str(manifest["topic"]) / ACTIVE_NAME,
            manifest_path,
            "restored",
        )
    if phase != "committed":
        raise SparseIndexError("restore-requires-committed")
    return _rollback(
        manifest_path,
        docs_dir,
        durability=_operation_durability(durability),
        terminal_phase="restored",
        allow_committed=True,
    )


def purge_transaction(
    manifest_path: Path,
    docs_dir: Path,
    *,
    confirmation: str | None,
    manifest_sha256: str | None = None,
    durability: DurableIO | None = None,
) -> BuildResult:
    if not manifest_path.is_absolute():
        raise SparseIndexError("absolute-manifest-path-required")
    if manifest_sha256 is None or _sha256_file(manifest_path) != manifest_sha256:
        raise SparseIndexError("exact-manifest-hash-required")
    manifest, topic_dir, run_root = _load_manifest(
        manifest_path,
        docs_dir.resolve(),
    )
    if manifest.get("phase") != "committed":
        raise SparseIndexError("purge-requires-committed")
    if confirmation != manifest.get("run_id"):
        raise SparseIndexError("explicit-purge-confirmation-required")
    active = topic_dir / ACTIVE_NAME
    if not _candidate_matches(manifest, active):
        raise SparseIndexError("active-v2-drift")
    rows = [
        cast(dict[str, object], row)
        for row in cast(list[object], manifest["artifacts"])
    ]
    allowed = {
        manifest_path.resolve(),
        (run_root / "candidate").resolve(),
        (run_root / "legacy").resolve(),
        *(
            _row_paths(topic_dir, row)[1].resolve()
            for row in rows
        ),
    }
    for path in run_root.rglob("*"):
        if path.resolve() not in allowed and path.is_file():
            raise SparseIndexError(f"unexpected-journal-content:{path}")
    for row in rows:
        _original, quarantine = _row_paths(topic_dir, row)
        if (
            not quarantine.is_file()
            or quarantine.is_symlink()
            or _sha256_file(quarantine) != str(row["sha256"])
        ):
            raise SparseIndexError(f"quarantine-hash-mismatch:{quarantine}")
    durable = _operation_durability(durability)
    durable.preflight(
        topic_dir,
        [manifest_path, *(path for row in rows for path in _row_paths(topic_dir, row))],
    )
    _history(manifest, "purge_intent")
    _write_manifest(manifest_path, manifest, durable)
    for row in rows:
        _original, quarantine = _row_paths(topic_dir, row)
        quarantine.unlink()
    _history(manifest, "purged")
    _write_manifest(manifest_path, manifest, durable)
    return BuildResult(active, manifest_path, "purged")
