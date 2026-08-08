"""Transactional saved-auth Codex category-summary contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast, final
from unittest.mock import patch

from pipeline.lib.generation_cache import CacheIdentity, GenerationCache
from pipeline.runtime_policy import RuntimePolicy
from pipeline.schemas.codex_schema import JsonObject


module = importlib.import_module("pipeline.build_category_summaries")
SummaryCodexFactory = cast(
    Callable[..., object],
    getattr(module, "SummaryCodex"),
)
run_category_summary = cast(
    Callable[..., JsonObject],
    getattr(module, "_run_category_summary"),
)
SummaryGenerationError = cast(
    type[RuntimeError],
    getattr(module, "SummaryGenerationError"),
)
validate_description = cast(
    Callable[[str, str], str | None],
    getattr(module, "validate_description"),
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _identity_factory(**values: object) -> CacheIdentity:
    role = cast(str, values["role"])
    prompt = cast(str, values["prompt"])
    schema = cast(JsonObject, values["schema"])
    source = cast(bytes, values["source"])
    return CacheIdentity(
        runtime_mode="codex",
        capability="generation",
        role=role,
        model="gpt-5.6-luna",
        reasoning_effort="xhigh",
        cli_version="0.147.0",
        signed_binary_sha256="1" * 64,
        attestation_sha256="2" * 64,
        contract_sha256="3" * 64,
        policy_version="1",
        policy_sha256="4" * 64,
        prompt_version=cast(str, values["prompt_version"]),
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        schema_version=cast(str, values["schema_version"]),
        schema_sha256=_digest(schema),
        source_sha256=hashlib.sha256(source).hexdigest(),
        task_id=cast(str, values["task_id"]),
    )


def _valid_korean(label: str = "분류") -> str:
    return (
        f"{label} 연구는 과학 문제를 해결하기 위한 데이터와 모델의 결합을 다룹니다. "
        "핵심 방법은 재현 가능한 학습 절차와 구조화된 검증을 함께 적용하는 것입니다. "
        "대표 논문들은 예측 정확도뿐 아니라 일반화 가능성과 해석 가능성을 비교합니다. "
        "이러한 결과는 후속 연구의 신뢰성과 실제 적용 범위를 확장하는 기반이 됩니다."
    )


@final
class FakeGateway:
    def __init__(self, outputs: list[JsonObject]) -> None:
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def generate_json(self, role: str, prompt: str, schema: JsonObject) -> JsonObject:
        _ = (prompt, schema)
        self.calls.append(role)
        if not self.outputs:
            raise AssertionError("unexpected gateway call")
        return self.outputs.pop(0)


class SummaryGenerator(Protocol):
    calls: list[str]

    def generate_korean(
        self,
        *,
        prompt: str,
        source: JsonObject,
        task_id: str,
        label: str,
        max_attempts: int = 2,
    ) -> str: ...


@final
class StubGenerator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def generate_korean(
        self,
        *,
        prompt: str,
        source: JsonObject,
        task_id: str,
        label: str,
        max_attempts: int = 2,
    ) -> str:
        _ = (prompt, source, max_attempts)
        self.calls.append(task_id)
        if self.fail:
            raise SummaryGenerationError("invalid Korean twice")
        return _valid_korean(label)


def _papers(title: str = "Paper A") -> list[JsonObject]:
    papers: list[JsonObject] = []
    for number in range(1, 32):
        paper_title = title if number == 1 else f"Paper {number}"
        papers.append(cast(JsonObject, cast(object, {
            "classifications": {
                "fixture": {
                    "primary_category": "Scientific Agents",
                    "sub_category": "Verification",
                }
            },
            "date": f"2026-01-{min(number, 28):02d}",
            "essence": "검증 가능한 과학 에이전트를 구성한다.",
            "score": 6 - min(number, 5),
            "slug": f"{number:03d}_Paper",
            "title": paper_title,
            "topics": ["fixture"],
        })))
    return papers


@final
class CodexCategorySummaryTests(unittest.TestCase):
    def test_quality_gate_allows_numeric_citations_but_rejects_placeholder(self) -> None:
        valid = _valid_korean("인용") + " 근거는 [123]에서 확인됩니다."
        self.assertIsNone(validate_description(valid, "citation"))
        self.assertIsNotNone(
            validate_description(valid.replace("[123]", "[NNN]"), "citation")
        )

    def test_short_form_cache_reuse_source_invalidation_and_quality_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="category-summary-cache-") as directory:
            cache = GenerationCache(Path(directory) / ".llm_cache")
            gateway = FakeGateway(
                [
                    {"description_ko": _valid_korean("첫 생성")},
                    {"description_ko": _valid_korean("변경 생성")},
                ]
            )
            generator = cast(
                SummaryGenerator,
                SummaryCodexFactory(
                    RuntimePolicy("codex"),
                    gateway,
                    cache,
                    _identity_factory,
                ),
            )
            first = generator.generate_korean(
                prompt="요약",
                source={"classification": "a"},
                task_id="category-summary:fixture:a",
                label="A",
            )
            second = generator.generate_korean(
                prompt="요약",
                source={"classification": "a"},
                task_id="category-summary:fixture:a",
                label="A",
            )
            changed = generator.generate_korean(
                prompt="요약",
                source={"classification": "b"},
                task_id="category-summary:fixture:a",
                label="A",
            )
            self.assertEqual(first, second)
            self.assertNotEqual(first, changed)
            self.assertEqual(gateway.calls, ["short_form", "short_form"])

            retry_gateway = FakeGateway(
                [
                    {"description_ko": "too short"},
                    {"description_ko": _valid_korean("재시도")},
                ]
            )
            retry_generator = cast(
                SummaryGenerator,
                SummaryCodexFactory(
                    RuntimePolicy("codex"),
                    retry_gateway,
                    GenerationCache(Path(directory) / "retry-cache"),
                    _identity_factory,
                ),
            )
            retry_text = retry_generator.generate_korean(
                prompt="재시도 요약",
                source={"classification": "retry"},
                task_id="category-summary:fixture:retry",
                label="retry",
            )
            self.assertIn("재시도", retry_text)
            self.assertEqual(retry_gateway.calls, ["short_form", "short_form"])
            self.assertEqual(len(list((Path(directory) / "retry-cache").glob("*.json"))), 1)

    def test_terminal_invalid_korean_has_no_success_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="category-summary-invalid-") as directory:
            gateway = FakeGateway(
                [
                    {"description_ko": "invalid"},
                    {"description_ko": "still invalid"},
                ]
            )
            generator = cast(
                SummaryGenerator,
                SummaryCodexFactory(
                    RuntimePolicy("codex"),
                    gateway,
                    GenerationCache(Path(directory) / ".llm_cache"),
                    _identity_factory,
                ),
            )
            with self.assertRaises(SummaryGenerationError):
                _ = generator.generate_korean(
                    prompt="실패 요약",
                    source={"classification": "invalid"},
                    task_id="category-summary:fixture:invalid",
                    label="invalid",
                )
            self.assertEqual(gateway.calls, ["short_form", "short_form"])
            self.assertEqual(list((Path(directory) / ".llm_cache").glob("*.json")), [])

    def test_atomic_publish_exact_reuse_and_failure_preserves_prior(self) -> None:
        with tempfile.TemporaryDirectory(prefix="category-summary-stage-") as directory:
            root = Path(directory)
            papers_dir = root / "papers"
            topic_dir = root / "fixture"
            papers_dir.mkdir()
            topic_dir.mkdir()
            index_path = papers_dir / "_papers_index.json"
            _ = index_path.write_text(
                json.dumps(_papers(), ensure_ascii=False),
                encoding="utf-8",
            )
            summary_path = topic_dir / "_category_summaries.json"
            first_generator = StubGenerator()
            with (
                patch.object(module, "PAPERS_DIR", str(papers_dir)),
                patch.object(module, "get_topic_dir", return_value=topic_dir),
            ):
                first = run_category_summary(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                    generator=first_generator,
                )
                first_bytes = summary_path.read_bytes()
                second_generator = StubGenerator()
                second = run_category_summary(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                    generator=second_generator,
                )
                self.assertEqual(first["status"], "ok")
                self.assertEqual(second["reused"], 1)
                self.assertEqual(second_generator.calls, [])
                self.assertEqual(summary_path.read_bytes(), first_bytes)
                self.assertEqual(len(first_generator.calls), 2)
                self.assertFalse((topic_dir / "_search_index.json").exists())

                _ = index_path.write_text(
                    json.dumps(_papers("Changed Paper A"), ensure_ascii=False),
                    encoding="utf-8",
                )
                failed_cache = topic_dir / "failed-cache"
                failed_gateway = FakeGateway(
                    [
                        {"description_ko": "invalid"},
                        {"description_ko": "still invalid"},
                    ]
                )
                failed_generator = cast(
                    SummaryGenerator,
                    SummaryCodexFactory(
                        RuntimePolicy("codex"),
                        failed_gateway,
                        GenerationCache(failed_cache),
                        _identity_factory,
                    ),
                )
                failed = run_category_summary(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                    generator=failed_generator,
                )
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(
                    failed_gateway.calls,
                    ["short_form", "short_form"],
                )
                self.assertEqual(list(failed_cache.glob("*.json")), [])
                self.assertEqual(summary_path.read_bytes(), first_bytes)
                self.assertEqual(
                    (topic_dir / "_category_summaries.previous.json").read_bytes(),
                    first_bytes,
                )
                self.assertTrue(
                    (topic_dir / "_category_summaries.failed.json").exists()
                )

                resumed = run_category_summary(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                    generator=StubGenerator(),
                )
                self.assertEqual(resumed["status"], "ok")
                self.assertNotEqual(summary_path.read_bytes(), first_bytes)
                self.assertFalse(
                    (topic_dir / "_category_summaries.failed.json").exists()
                )
                self.assertEqual(list(topic_dir.glob("*.tmp")), [])


if __name__ == "__main__":
    _ = unittest.main()
