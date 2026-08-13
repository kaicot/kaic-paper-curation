"""Structured Korean review generation through the saved-auth Codex boundary."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Callable, Protocol, cast, final
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

from pipeline.lib.generation_cache import CacheIdentity, GenerationCache
from pipeline.providers.codex_gateway import CodexGateway, CodexGatewayError
from pipeline.runtime_policy import resolve_runtime_policy
from pipeline.schemas.codex_schema import JsonObject


class ReviewModule(Protocol):
    PAPERS_DIR: str
    time: ModuleType

    def write_review(self, *args: object, **kwargs: object) -> bool: ...
    def process_paper(self, *args: object, **kwargs: object) -> str: ...


class RendererModule(Protocol):
    def convert_review(self, md_path: str, topic: str, slug_dir: str) -> str: ...


_review_module = importlib.import_module("pipeline.run_update_force")
review = cast(ReviewModule, cast(object, _review_module))
do_process = cast(
    Callable[..., tuple[str, str]],
    getattr(_review_module, "_do_process"),
)
renderer = cast(
    RendererModule,
    cast(object, importlib.import_module("pipeline.review_to_html")),
)


def _identity(**changes: str) -> CacheIdentity:
    values = {
        "runtime_mode": "codex",
        "capability": "generation",
        "role": "long_form",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "xhigh",
        "cli_version": "0.147.0",
        "signed_binary_sha256": "1" * 64,
        "attestation_sha256": "2" * 64,
        "contract_sha256": "3" * 64,
        "policy_version": "1",
        "policy_sha256": "4" * 64,
        "prompt_version": "review-v1",
        "prompt_sha256": "5" * 64,
        "schema_version": "review-v1",
        "schema_sha256": "6" * 64,
        "source_sha256": "7" * 64,
        "task_id": "review:001",
    }
    values.update(changes)
    return CacheIdentity(**values)


def _payload() -> JsonObject:
    return {
        "essence": "이 논문은 과학적 발견을 위한 agent workflow의 핵심 구조를 제안한다.",
        "fig_essence": 1,
        "known": "기존 방법은 고정된 단일 단계 최적화에 의존한다.",
        "gap": "복합 연구 과정의 오류 전파를 제어하는 방법이 부족하다.",
        "why": "재현 가능한 자동화 연구를 위해 이 문제는 중요하다.",
        "approach": "검증 가능한 planner와 executor를 단계적으로 결합한다.",
        "achievement": "1. **정확도**: 여러 benchmark에서 안정적인 향상을 보인다.",
        "fig_achievement": 0,
        "how": "- planner가 가설을 생성한다.\n- verifier가 근거를 검사한다.",
        "fig_how": 0,
        "originality": "- 구조화된 검증 단계를 agent loop에 통합한다.",
        "limitation": "- 제한된 domain에서만 평가되어 추가 검증이 필요하다.",
        "novelty": 4,
        "technical": 4,
        "significance": 5,
        "clarity": 4,
        "overall": 4,
        "verdict": "구조적 검증을 통해 자동 연구의 신뢰성을 높인 중요한 연구다.",
    }


@final
class FakeReviewGateway:
    def __init__(
        self,
        result: JsonObject | None = None,
        error: CodexGatewayError | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.roles: list[str] = []

    def generate_json(self, role: str, prompt: str, schema: JsonObject) -> JsonObject:
        self.calls += 1
        self.roles.append(role)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("fake result missing")
        self.assert_strict_schema(schema)
        if "논문" not in prompt:
            raise AssertionError("review prompt omitted paper context")
        return self.result

    @staticmethod
    def assert_strict_schema(schema: JsonObject) -> None:
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise AssertionError("review schema is not strict")


@final
class CodexReviewTests(unittest.TestCase):
    def make_paper(self, root: Path) -> tuple[JsonObject, Path]:
        slug_dir = root / "001_fixture"
        slug_dir.mkdir()
        _ = (slug_dir / "text.md").write_text(
            "# Paper\n\n검증 가능한 scientific agent workflow를 제시한다. " * 40,
            encoding="utf-8",
        )
        item: JsonObject = {
            "title": "검증 가능한 Scientific Agent",
            "creators": [{"firstName": "Ada", "lastName": "Kim"}],
            "date": "2026",
            "DOI": "10.1000/fixture",
            "abstractNote": "구조화된 검증을 다룬다.",
        }
        return item, slug_dir

    def test_review_v1_schema_is_exact_and_strict(self) -> None:
        schema_path = ROOT / "pipeline/schemas/review-v1.json"
        schema = cast(JsonObject, json.loads(schema_path.read_text(encoding="utf-8")))
        expected = {
            "essence", "fig_essence", "known", "gap", "why", "approach",
            "achievement", "fig_achievement", "how", "fig_how",
            "originality", "limitation", "novelty", "technical",
            "significance", "clarity", "overall", "verdict",
        }
        self.assertEqual(schema["type"], "object")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(cast(list[str], schema["required"])), expected)
        self.assertEqual(set(cast(dict[str, object], schema["properties"])), expected)

    def test_counted_codex_cache_formats_and_renders_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-review-") as directory:
            root = Path(directory)
            item, slug_dir = self.make_paper(root)
            figures = [{"name": "1", "caption": "Agent workflow overview"}]
            gateway = FakeReviewGateway(_payload())
            cache = GenerationCache(slug_dir / ".llm_cache")

            first = review.write_review(
                item,
                str(slug_dir),
                figures,
                gateway=gateway,
                cache=cache,
                identity=_identity(),
            )
            second = review.write_review(
                item,
                str(slug_dir),
                figures,
                gateway=gateway,
                cache=cache,
                identity=_identity(),
            )

            self.assertTrue(first)
            self.assertTrue(second)
            self.assertEqual(gateway.calls, 1)
            self.assertEqual(gateway.roles, ["long_form"])
            review_path = slug_dir / "review.md"
            review_text = review_path.read_text(encoding="utf-8")
            self.assertIn("## Essence", review_text)
            self.assertIn("## Evaluation", review_text)
            self.assertIn("- Technical Soundness: 4/5", review_text)
            self.assertIn("구조적 검증", review_text)
            self.assertFalse(any(slug_dir.glob(".review.md.*.tmp")))

            html = renderer.convert_review(
                str(review_path),
                "fixture",
                str(slug_dir),
            )
            self.assertIn("검증 가능한 Scientific Agent", html)
            self.assertIn("Technical Soundness", html)
            self.assertIn("saved-auth Codex", html)
            self.assertNotIn("Claude", html)

    def test_typed_gateway_and_schema_failures_never_replace_review(self) -> None:
        failures: list[FakeReviewGateway] = [
            FakeReviewGateway(error=CodexGatewayError("auth-required", "saved auth missing")),
            FakeReviewGateway(error=CodexGatewayError("quota-exhausted", "quota unavailable")),
            FakeReviewGateway(error=CodexGatewayError("schema-invalid", "invalid JSON")),
            FakeReviewGateway({"essence": "누락된 payload"}),
            FakeReviewGateway({**_payload(), "essence": "English only structured result"}),
            FakeReviewGateway({**_payload(), "overall": 6}),
        ]
        for index, gateway in enumerate(failures):
            with self.subTest(index=index), tempfile.TemporaryDirectory(
                prefix="codex-review-failure-"
            ) as directory:
                item, slug_dir = self.make_paper(Path(directory))
                review_path = slug_dir / "review.md"
                old = b"# existing valid review\n\n## Essence\n\nbyte identical\n"
                _ = review_path.write_bytes(old)

                result = review.write_review(
                    item,
                    str(slug_dir),
                    [],
                    gateway=gateway,
                    cache=GenerationCache(slug_dir / ".llm_cache"),
                    identity=_identity(task_id=f"review:failure:{index}"),
                )

                self.assertFalse(result)
                self.assertEqual(review_path.read_bytes(), old)
                self.assertEqual(gateway.calls, 1)
                cache_files = list((slug_dir / ".llm_cache").glob("*.json"))
                self.assertEqual(cache_files, [])

    def test_off_policy_denies_before_gateway_construction_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-review-off-") as directory:
            item, slug_dir = self.make_paper(Path(directory))
            review_path = slug_dir / "review.md"
            old = b"# existing review\n\n## Essence\n\npreserved\n"
            _ = review_path.write_bytes(old)
            off_policy = resolve_runtime_policy(
                {"schema_version": 2, "runtime": {"llm_mode": "off"}}
            )
            with patch.object(
                CodexGateway,
                "production",
                side_effect=AssertionError("gateway must not be constructed"),
            ):
                result = review.write_review(
                    item,
                    str(slug_dir),
                    [],
                    runtime_policy=off_policy,
                )
            self.assertFalse(result)
            self.assertEqual(review_path.read_bytes(), old)
            self.assertFalse((slug_dir / ".llm_cache").exists())

    def test_do_process_stops_before_render_when_review_generation_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-review-process-") as directory:
            item, slug_dir = self.make_paper(Path(directory))
            old = b"# existing review\n\n## Essence\n\npreserved\n"
            _ = (slug_dir / "review.md").write_bytes(old)
            renderer = Mock()
            with (
                patch.object(review, "extract_text", return_value=True),
                patch.object(review, "_zotero_text_sanity", return_value=(True, "")),
                patch.object(review, "extract_figures", return_value=[]),
                patch.object(review, "write_review", return_value=False),
                patch.object(review, "convert_to_html", renderer),
            ):
                status, reason = do_process(
                    item,
                    "001_fixture",
                    str(slug_dir),
                    str(slug_dir / "fixture.pdf"),
                    None,
                )
            self.assertEqual((status, reason), ("fail", "review_write_failed"))
            self.assertEqual((slug_dir / "review.md").read_bytes(), old)
            renderer.assert_not_called()

    def test_process_cleanup_preserves_existing_review_before_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-review-cleanup-") as directory:
            item, slug_dir = self.make_paper(Path(directory))
            old = b"# existing review\n\n## Essence\n\npreserved\n"
            review_path = slug_dir / "review.md"
            _ = review_path.write_bytes(old)
            _ = (slug_dir / "index.html").write_text("existing html", encoding="utf-8")
            observed: list[bytes] = []

            def fail_after_cleanup(*_args: object) -> tuple[str, str]:
                observed.append(review_path.read_bytes())
                return "fail", "review_write_failed"

            checkpoint: JsonObject = {"completed": [], "failed": [], "phase": "fixture"}
            with (
                patch.object(review, "PAPERS_DIR", str(Path(directory))),
                patch.object(review, "paper_has_other_topics", return_value=False),
                patch.object(
                    review,
                    "find_pdf",
                    return_value=(str(slug_dir / "fixture.pdf"), "fixture"),
                ),
                patch.object(review, "_do_process", side_effect=fail_after_cleanup),
                patch.object(review, "save_checkpoint", return_value=None),
                patch.object(review.time, "sleep", return_value=None),
            ):
                result = review.process_paper(
                    item,
                    "001_fixture",
                    checkpoint,
                    None,
                )

            self.assertEqual(result, "review_write_failed")
            self.assertEqual(observed, [old, old, old])
            self.assertEqual(review_path.read_bytes(), old)


if __name__ == "__main__":
    _ = unittest.main()
