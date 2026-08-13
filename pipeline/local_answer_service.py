"""Strict local BM25 evidence selection and saved-auth answer generation."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, final

from pipeline.lib.generation_cache import (
    CacheFailure,
    CacheIdentity,
    CacheSuccess,
    GenerationCache,
    GenerationCacheError,
)
from pipeline.lib.local_answer_contract import (
    LENGTH_VALUES,
    MAX_CITATIONS,
    MAX_CONTEXT_CHARS,
    MAX_QUERY_CHARS,
    MAX_RESPONSE_BYTES,
    MAX_RETRIEVED_CHUNKS,
    RESPONSE_SCHEMA,
    RESPONSE_SCHEMA_PATH,
    TOPIC_RE,
)
from pipeline.lib.run_state import TopicBusyError, TopicLock
from pipeline.providers.codex_gateway import CodexGateway, CodexGatewayError
from pipeline.query_search_index import query_search_index
from pipeline.runtime_policy import RuntimePolicy
from pipeline.schemas.codex_schema import JsonObject, JsonValue
from pipeline.sparse_index import tokenize

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_REFERENCE_RE = re.compile(r"\[ref:([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    ref: int
    slug: str
    section: str
    text: str


class AnswerGenerator(Protocol):
    def generate(
        self,
        *,
        topic: str,
        query: str,
        length: str,
        chunks: list[EvidenceChunk],
    ) -> Mapping[str, object]: ...


@final
class LocalAnswerError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status: int = status
        self.code: str = code


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


def _strict_json(path: Path) -> JsonObject:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    value = cast(
        object,
        json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite constant: {token}")
            ),
        ),
    )
    if not isinstance(value, dict):
        raise ValueError("schema root must be an object")
    return cast(JsonObject, cast(object, value))


def _sections(markdown: str) -> list[tuple[str, str]]:
    matches = list(_SECTION_RE.finditer(markdown))
    rows: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        text = markdown[start:end].strip()
        if text and name != "Related Papers":
            rows.append((name, text))
    return rows


def validate_local_answer_response(
    value: Mapping[str, object],
    chunks: list[EvidenceChunk],
) -> JsonObject:
    if set(value) != {
        "answer",
        "citations",
        "schema",
        "schema_version",
    }:
        raise LocalAnswerError(502, "response-schema-invalid")
    if value.get("schema") != RESPONSE_SCHEMA or value.get("schema_version") != 1:
        raise LocalAnswerError(502, "response-schema-invalid")
    try:
        encoded_size = len(_canonical_json(value))
    except (TypeError, ValueError) as error:
        raise LocalAnswerError(502, "response-schema-invalid") from error
    if encoded_size > MAX_RESPONSE_BYTES:
        raise LocalAnswerError(413, "response-too-large")
    answer = value.get("answer")
    citations = value.get("citations")
    if not isinstance(answer, str) or not answer.strip():
        raise LocalAnswerError(502, "response-schema-invalid")
    if len(answer) > 30_000:
        raise LocalAnswerError(502, "response-schema-invalid")
    if not isinstance(citations, list):
        raise LocalAnswerError(502, "response-schema-invalid")
    citation_rows = cast(list[object], citations)
    if not 1 <= len(citation_rows) <= MAX_CITATIONS:
        raise LocalAnswerError(502, "response-schema-invalid")
    approved = {
        chunk.ref: (chunk.slug, chunk.section)
        for chunk in chunks
    }
    structured: set[int] = set()
    for raw in cast(list[JsonValue], citation_rows):
        if not isinstance(raw, dict):
            raise LocalAnswerError(502, "citation-invalid")
        citation = cast(JsonObject, raw)
        if set(citation) != {"ref", "section", "slug"}:
            raise LocalAnswerError(502, "citation-invalid")
        ref = citation.get("ref")
        slug = citation.get("slug")
        section = citation.get("section")
        if (
            not isinstance(ref, int)
            or isinstance(ref, bool)
            or ref in structured
            or approved.get(ref) != (slug, section)
        ):
            raise LocalAnswerError(502, "citation-invalid")
        structured.add(ref)
    textual: set[int] = set()
    tokens = cast(list[str], _REFERENCE_RE.findall(answer))
    for token in tokens:
        if not token.isascii() or not token.isdigit():
            raise LocalAnswerError(502, "citation-invalid")
        textual.add(int(token))
    if textual != structured:
        raise LocalAnswerError(502, "citation-invalid")
    return cast(JsonObject, cast(object, dict(value)))


@final
class LocalAnswerCodex:
    """Generation-cache adapter for the attested saved-auth gateway."""

    def __init__(
        self,
        gateway: CodexGateway,
        runtime_policy: RuntimePolicy,
        cache_root: Path,
    ) -> None:
        self.gateway: CodexGateway = gateway
        self.runtime_policy: RuntimePolicy = runtime_policy
        self.cache_root: Path = cache_root
        self.schema: JsonObject = _strict_json(RESPONSE_SCHEMA_PATH)

    @classmethod
    def production(
        cls,
        repository: Path,
        runtime_policy: RuntimePolicy,
        cache_root: Path,
    ) -> "LocalAnswerCodex":
        if runtime_policy.mode != "codex":
            raise LocalAnswerError(503, "runtime-off")
        return cls(
            CodexGateway.production(repository),
            runtime_policy,
            cache_root,
        )

    def generate(
        self,
        *,
        topic: str,
        query: str,
        length: str,
        chunks: list[EvidenceChunk],
    ) -> JsonObject:
        evidence = [
            {
                "ref": chunk.ref,
                "section": chunk.section,
                "slug": chunk.slug,
                "text": chunk.text,
            }
            for chunk in chunks
        ]
        source = _canonical_json(
            {
                "evidence": evidence,
                "length": length,
                "query": query,
                "topic": topic,
            }
        )
        prompt = "\n".join(
            (
                "아래 로컬 근거만 사용해 한국어로 답하세요.",
                "모든 사실 문장에 실제 숫자를 넣은 ref 인용을 붙이고,",
                "예를 들어 근거 1은 [ref:1]로 인용하세요.",
                "제공되지 않은 ref/slug/section은 절대 만들지 마세요.",
                "인용 형식 자체를 설명하거나 N 같은 placeholder를 쓰지 마세요.",
                f"답변 길이: {length}",
                f"질문: {query}",
                "근거 JSON:",
                source.decode("utf-8"),
            )
        )
        identity = CacheIdentity.from_gateway(
            runtime_policy=self.runtime_policy,
            gateway=self.gateway,
            role="long_form",
            prompt_version="local-answer-prompt-v1",
            prompt=prompt,
            schema_version=RESPONSE_SCHEMA,
            schema=self.schema,
            source=source,
            task_id=(
                f"local-answer:{topic}:"
                f"{hashlib.sha256(source).hexdigest()}"
            ),
        )
        cache = GenerationCache(
            self.cache_root / topic / ".llm_cache"
        )

        def produce() -> CacheSuccess | CacheFailure:
            active_prompt = prompt
            for attempt in range(2):
                try:
                    generated = self.gateway.generate_json(
                        "long_form",
                        active_prompt,
                        self.schema,
                    )
                    validated = validate_local_answer_response(
                        generated,
                        chunks,
                    )
                    return CacheSuccess(validated)
                except LocalAnswerError as error:
                    if error.status == 413:
                        raise
                    if attempt == 0:
                        active_prompt = "\n".join(
                            (
                                prompt,
                                "이전 출력의 인용 또는 schema가 무효였습니다.",
                                "실제 근거 번호만 사용해 전체 JSON을 다시 작성하세요.",
                            )
                        )
                        continue
                    return CacheFailure("failed")
                except (CodexGatewayError, ValueError):
                    return CacheFailure("failed")
            return CacheFailure("failed")

        try:
            result = cache.get_or_generate(identity, produce)
            return validate_local_answer_response(result, chunks)
        except LocalAnswerError:
            raise
        except GenerationCacheError as error:
            raise LocalAnswerError(502, "generation-failed") from error


@final
class LocalAnswerService:
    def __init__(
        self,
        docs_dir: Path,
        generator: AnswerGenerator,
        generation_lock: threading.Lock | None = None,
    ) -> None:
        self.docs_dir: Path = docs_dir.resolve()
        self.generator: AnswerGenerator = generator
        self.generation_lock: threading.Lock = (
            generation_lock or threading.Lock()
        )

    def _validate_request(
        self,
        request: Mapping[str, object],
    ) -> tuple[str, str, str]:
        if "topic" not in request:
            raise LocalAnswerError(404, "topic-required")
        if set(request) != {"length", "query", "topic"}:
            raise LocalAnswerError(400, "request-schema-invalid")
        topic = request.get("topic")
        query = request.get("query")
        length = request.get("length")
        if not isinstance(topic, str) or TOPIC_RE.fullmatch(topic) is None:
            raise LocalAnswerError(422, "topic-invalid")
        if not isinstance(length, str) or length not in LENGTH_VALUES:
            raise LocalAnswerError(422, "length-invalid")
        if not isinstance(query, str):
            raise LocalAnswerError(400, "request-schema-invalid")
        query = query.strip()
        if not query:
            raise LocalAnswerError(422, "query-required")
        if len(query) > MAX_QUERY_CHARS:
            raise LocalAnswerError(413, "query-too-large")
        topic_dir = self.docs_dir / topic
        if (
            not topic_dir.is_dir()
            or topic_dir.is_symlink()
            or topic_dir.resolve().parent != self.docs_dir
        ):
            raise LocalAnswerError(404, "topic-not-found")
        return topic, query, length

    def _retrieve(self, topic: str, query: str) -> list[EvidenceChunk]:
        result = query_search_index(
            topic,
            query,
            top_k=MAX_RETRIEVED_CHUNKS,
            docs_dir=self.docs_dir,
        )
        if result.get("status") != "ok":
            status = result.get("status")
            if status in {"index-not-found", "empty-index"}:
                raise LocalAnswerError(404, "topic-not-found")
            raise LocalAnswerError(502, "retrieval-failed")
        rows = cast(
            list[dict[str, object]],
            result.get("results", []),
        )
        try:
            index = _strict_json(
                self.docs_dir / topic / "_search_index.json"
            )
            source = cast(JsonObject, index["source"])
            review_rows = cast(list[JsonObject], source["reviews"])
            review_hashes = {
                cast(str, row["path"]): cast(str, row["sha256"])
                for row in review_rows
            }
        except (KeyError, OSError, TypeError, ValueError) as error:
            raise LocalAnswerError(502, "retrieval-failed") from error
        query_terms = set(tokenize(query))
        candidates: list[tuple[int, int, int, str, str, str]] = []
        for rank, row in enumerate(rows):
            slug = row.get("slug")
            if (
                not isinstance(slug, str)
                or Path(slug).name != slug
                or slug in {"", ".", ".."}
            ):
                raise LocalAnswerError(502, "retrieval-failed")
            review = self.docs_dir / "papers" / slug / "review.md"
            if (
                not review.is_file()
                or review.is_symlink()
                or self.docs_dir not in review.resolve().parents
            ):
                raise LocalAnswerError(502, "retrieval-failed")
            try:
                raw_review = review.read_bytes()
                markdown = raw_review.decode("utf-8")
            except (OSError, UnicodeError) as error:
                raise LocalAnswerError(502, "retrieval-failed") from error
            relative = review.relative_to(self.docs_dir).as_posix()
            if (
                review_hashes.get(relative)
                != hashlib.sha256(raw_review).hexdigest()
            ):
                raise LocalAnswerError(502, "retrieval-failed")
            for section_index, (section, text) in enumerate(
                _sections(markdown)
            ):
                counts = Counter(tokenize(text))
                overlap = sum(counts[term] for term in query_terms)
                candidates.append(
                    (
                        -overlap,
                        rank,
                        section_index,
                        slug,
                        section,
                        text,
                    )
                )
        candidates.sort()
        selected: list[EvidenceChunk] = []
        remaining = MAX_CONTEXT_CHARS
        for _, _, _, slug, section, text in candidates:
            if len(selected) >= MAX_RETRIEVED_CHUNKS or remaining <= 0:
                break
            bounded = text[: min(3_000, remaining)].strip()
            if not bounded:
                continue
            selected.append(
                EvidenceChunk(
                    ref=len(selected) + 1,
                    section=section,
                    slug=slug,
                    text=bounded,
                )
            )
            remaining -= len(bounded)
        if not selected:
            raise LocalAnswerError(422, "evidence-not-found")
        return selected

    def answer(self, request: Mapping[str, object]) -> JsonObject:
        topic, query, length = self._validate_request(request)
        if not self.generation_lock.acquire(blocking=False):
            raise LocalAnswerError(409, "generation-busy")
        topic_lock: TopicLock | None = None
        try:
            try:
                topic_lock = TopicLock.acquire(
                    self.docs_dir
                    / ".local-answer-locks"
                    / f"{topic}.lock",
                    topic,
                )
            except TopicBusyError as error:
                raise LocalAnswerError(409, "generation-busy") from error
            chunks = self._retrieve(topic, query)
            try:
                generated = self.generator.generate(
                    topic=topic,
                    query=query,
                    length=length,
                    chunks=chunks,
                )
            except LocalAnswerError:
                raise
            except Exception as error:
                raise LocalAnswerError(502, "generation-failed") from error
            return validate_local_answer_response(generated, chunks)
        finally:
            if topic_lock is not None:
                topic_lock.release()
            self.generation_lock.release()

    def probe_citation_mismatch(
        self,
        request: Mapping[str, object],
    ) -> JsonObject:
        topic, query, _length = self._validate_request(request)
        chunks = self._retrieve(topic, query)
        return validate_local_answer_response(
            {
                "answer": "invalid [ref:9]",
                "citations": [
                    {
                        "ref": 9,
                        "section": "Unknown",
                        "slug": "999_Missing",
                    }
                ],
                "schema": RESPONSE_SCHEMA,
                "schema_version": 1,
            },
            chunks,
        )
