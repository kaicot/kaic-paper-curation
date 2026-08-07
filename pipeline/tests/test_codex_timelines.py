"""Strict cached text timeline generation without image imports."""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import subprocess
import sys
import tempfile
import types
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast, final, override
from unittest.mock import patch

from pipeline.lib.generation_cache import (
    CacheIdentity,
    GenerationCache,
    GenerationCacheError,
)
from pipeline.runtime_policy import RuntimePolicy
from pipeline.schemas.codex_schema import JsonObject


timeline_module = importlib.import_module("pipeline.generate_timelines")
timeline_codex_factory = cast(
    Callable[..., object],
    getattr(timeline_module, "TimelineCodex"),
)
run_timeline = cast(
    Callable[..., JsonObject],
    getattr(timeline_module, "_run_timeline"),
)
TimelineGenerationError = cast(
    type[RuntimeError],
    getattr(timeline_module, "TimelineGenerationError"),
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
        model="gpt-5.6-terra",
        reasoning_effort="xhigh",
        cli_version="0.146.1",
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


def _category_result(category: str, count: int, suffix: str = "") -> JsonObject:
    return cast(
        JsonObject,
        cast(
            object,
            {
                "category": category,
                "current_state_summary": (
                    f"{category} research currently combines validated methods, "
                    f"reproducible evaluation, and increasingly integrated systems {suffix}."
                ),
                "emergence_decline": [],
                "interactions": [],
                "milestones": [
                    {
                        "description": "A reproducible benchmark was established.",
                        "label": "Benchmark",
                        "type": "Turning Point",
                        "year": "2024",
                    }
                ],
                "paper_count": count,
                "sub_categories": [
                    {"name": "Verification", "paper_count": count}
                ],
                "sub_themes": [
                    {
                        "end": "2026",
                        "key_developments": ["reproducible benchmark"],
                        "name": "Verification",
                        "paper_count": count,
                        "relative_size": "LARGE",
                        "representative_tools": ["evaluation harness"],
                        "start": "2023",
                        "status": "ACCELERATING",
                    }
                ],
                "time_span": "2023-2026",
            },
        ),
    )


def _executive_result(suffix: str = "") -> JsonObject:
    text = (
        "이 연구 분야는 초기의 개별 예측 모형에서 출발하여 재현 가능한 평가와 "
        "통합형 과학 시스템으로 발전했습니다. 최근에는 검증 절차와 구조화된 데이터가 "
        "결합되면서 방법의 신뢰성과 일반화 가능성이 함께 향상되고 있습니다. 앞으로는 "
        f"분야 간 연결과 실제 과학 문제에서의 지속적인 검증이 핵심 방향이 될 것입니다{suffix}."
    )
    return {"executive_summary_ko": text}


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


class TimelineGenerator(Protocol):
    calls: list[str]

    def generate(
        self,
        *,
        prompt: str,
        schema: JsonObject,
        schema_version: str,
        prompt_version: str,
        source: JsonObject,
        task_id: str,
        validator: Callable[[JsonObject], JsonObject],
    ) -> JsonObject: ...


@final
class StubGenerator:
    def __init__(self, *, fail_category: str | None = None) -> None:
        self.fail_category = fail_category
        self.calls: list[str] = []

    def generate(
        self,
        *,
        prompt: str,
        schema: JsonObject,
        schema_version: str,
        prompt_version: str,
        source: JsonObject,
        task_id: str,
        validator: Callable[[JsonObject], JsonObject],
    ) -> JsonObject:
        _ = (prompt, schema, prompt_version)
        self.calls.append(task_id)
        if schema_version == "timeline-category-v1":
            category = cast(str, source["category"])
            if category == self.fail_category:
                raise TimelineGenerationError("fixture failure")
            papers = cast(
                list[JsonObject],
                cast(object, source["papers"]),
            )
            value = _category_result(
                category,
                cast(int, source["paper_count"]),
                cast(str, papers[0]["title"]),
            )
        else:
            value = _executive_result()
        return validator(value)


def _papers(title: str = "Agent Paper") -> list[JsonObject]:
    rows: list[JsonObject] = []
    for category, prefix in (("Agents", "A"), ("Models", "M")):
        for number in range(1, 3):
            rows.append(
                cast(
                    JsonObject,
                    cast(
                        object,
                        {
                            "classifications": {
                                "fixture": {
                                    "primary_category": category,
                                    "sub_category": "Verification",
                                }
                            },
                            "date": f"202{number + 3}-01-01",
                            "essence": "검증 가능한 방법을 제안한다.",
                            "id": f"{prefix}{number}",
                            "slug": f"{prefix}{number}_Paper",
                            "title": title if category == "Agents" and number == 1 else f"{category} {number}",
                            "topics": ["fixture"],
                        },
                    ),
                )
            )
    return rows


@final
class PoisonImageFinder(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.events = 0
        self.blocked = {
            "pipeline.lib." + "paper" + "banana",
            "lib." + "paper" + "banana",
            "P" + "IL",
        }

    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        _ = (path, target)
        if fullname in self.blocked:
            self.events += 1
            raise AssertionError(f"blocked image import: {fullname}")
        return None


@final
class CodexTimelineTests(unittest.TestCase):
    def test_module_import_is_structurally_image_free(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = """
import importlib.abc
import sys

sys.path.insert(0, sys.argv[1])
events = []
blocked = {
    "pipeline.lib." + "paper" + "banana",
    "lib." + "paper" + "banana",
    "P" + "IL",
}

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            events.append(fullname)
            raise AssertionError(fullname)
        return None

sys.meta_path.insert(0, Finder())
import pipeline.generate_timelines
assert events == [], events
"""
        completed = subprocess.run(
            [sys.executable, "-W", "error", "-c", script, str(root)],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def test_schema_invalid_result_has_no_success_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="timeline-invalid-") as directory:
            cache_path = Path(directory) / ".llm_cache"
            gateway = FakeGateway([_category_result("Wrong", 2)])
            generator = cast(
                TimelineGenerator,
                timeline_codex_factory(
                    RuntimePolicy("codex"),
                    gateway,
                    GenerationCache(cache_path),
                    _identity_factory,
                ),
            )
            schema = cast(JsonObject, getattr(timeline_module, "CATEGORY_SCHEMA"))
            with self.assertRaises(GenerationCacheError):
                _ = generator.generate(
                    prompt="invalid category",
                    schema=schema,
                    schema_version="timeline-category-v1",
                    prompt_version="timeline-category-prompt-v1",
                    source={"category": "Agents"},
                    task_id="timeline-category:fixture:invalid",
                    validator=lambda value: cast(
                        JsonObject,
                        getattr(timeline_module, "_validate_category")(
                            value,
                            "Agents",
                            2,
                        ),
                    ),
                )
            self.assertEqual(gateway.calls, ["long_form"])
            self.assertEqual(list(cache_path.glob("*.json")), [])

    def test_long_form_schema_cache_and_source_invalidation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="timeline-cache-") as directory:
            gateway = FakeGateway(
                [
                    _category_result("Agents", 2, "first"),
                    _category_result("Agents", 2, "changed"),
                    _executive_result(),
                ]
            )
            generator = cast(
                TimelineGenerator,
                timeline_codex_factory(
                    RuntimePolicy("codex"),
                    gateway,
                    GenerationCache(Path(directory) / ".llm_cache"),
                    _identity_factory,
                ),
            )
            schema = cast(JsonObject, getattr(timeline_module, "CATEGORY_SCHEMA"))

            def validator(value: JsonObject) -> JsonObject:
                return cast(
                    JsonObject,
                    getattr(timeline_module, "_validate_category")(
                        value,
                        "Agents",
                        2,
                    ),
                )
            first = generator.generate(
                prompt="category",
                schema=schema,
                schema_version="timeline-category-v1",
                prompt_version="timeline-category-prompt-v1",
                source={"category": "Agents", "version": 1},
                task_id="timeline-category:fixture:agents",
                validator=validator,
            )
            second = generator.generate(
                prompt="category",
                schema=schema,
                schema_version="timeline-category-v1",
                prompt_version="timeline-category-prompt-v1",
                source={"category": "Agents", "version": 1},
                task_id="timeline-category:fixture:agents",
                validator=validator,
            )
            changed = generator.generate(
                prompt="category",
                schema=schema,
                schema_version="timeline-category-v1",
                prompt_version="timeline-category-prompt-v1",
                source={"category": "Agents", "version": 2},
                task_id="timeline-category:fixture:agents",
                validator=validator,
            )
            executive_schema = cast(
                JsonObject,
                getattr(timeline_module, "EXECUTIVE_SCHEMA"),
            )
            _ = generator.generate(
                prompt="executive",
                schema=executive_schema,
                schema_version="timeline-executive-v1",
                prompt_version="timeline-executive-prompt-v1",
                source={"categories": [changed]},
                task_id="timeline-executive:fixture",
                validator=cast(
                    Callable[[JsonObject], JsonObject],
                    getattr(timeline_module, "_validate_executive"),
                ),
            )
            self.assertEqual(first, second)
            self.assertNotEqual(first, changed)
            self.assertEqual(gateway.calls, ["long_form", "long_form", "long_form"])

    def test_text_only_changed_category_merge_and_failure_preservation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="timeline-stage-") as directory:
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
            poison = PoisonImageFinder()
            sys.meta_path.insert(0, poison)
            try:
                with (
                    patch.object(timeline_module, "PAPERS_DIR", str(papers_dir)),
                    patch.object(
                        timeline_module,
                        "get_topic_dir",
                        return_value=topic_dir,
                    ),
                ):
                    first_generator = StubGenerator()
                    first = run_timeline(
                        "fixture",
                        runtime_policy=RuntimePolicy("codex"),
                        generator=first_generator,
                    )
                    category_path = topic_dir / "_category_narratives.json"
                    timeline_path = topic_dir / "_timeline_narrative.json"
                    first_category_bytes = category_path.read_bytes()
                    first_timeline_bytes = timeline_path.read_bytes()
                    first_rows = cast(
                        list[JsonObject],
                        json.loads(first_category_bytes),
                    )
                    source_hashes = [
                        row.get("source_sha256")
                        for row in first_rows
                    ]
                    self.assertTrue(
                        all(
                            isinstance(value, str) and len(value) == 64
                            for value in source_hashes
                        )
                    )
                    models_before = next(
                        row for row in first_rows if row["category"] == "Models"
                    )

                    _ = index_path.write_text(
                        json.dumps(_papers("Changed Agent Paper"), ensure_ascii=False),
                        encoding="utf-8",
                    )
                    changed_generator = StubGenerator()
                    changed = run_timeline(
                        "fixture",
                        categories=["Agents"],
                        images="skip",
                        runtime_policy=RuntimePolicy("codex"),
                        generator=changed_generator,
                    )
                    changed_rows = cast(
                        list[JsonObject],
                        json.loads(category_path.read_bytes()),
                    )
                    models_after = next(
                        row for row in changed_rows if row["category"] == "Models"
                    )
                    changed_category_bytes = category_path.read_bytes()
                    changed_timeline_bytes = timeline_path.read_bytes()

                    failed = run_timeline(
                        "fixture",
                        categories=["Agents"],
                        force_narrative=True,
                        images="skip",
                        runtime_policy=RuntimePolicy("codex"),
                        generator=StubGenerator(fail_category="Agents"),
                    )
                self.assertEqual(first["status"], "ok")
                self.assertEqual(changed["status"], "ok")
                self.assertEqual(changed["generated"], 1)
                self.assertEqual(changed["reused"], 1)
                self.assertEqual(models_before, models_after)
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(category_path.read_bytes(), changed_category_bytes)
                self.assertEqual(timeline_path.read_bytes(), changed_timeline_bytes)
                self.assertNotEqual(first_category_bytes, changed_category_bytes)
                self.assertNotEqual(first_timeline_bytes, changed_timeline_bytes)
                self.assertEqual(poison.events, 0)
                self.assertEqual(list(root.rglob("*.png")), [])
                self.assertEqual(list(root.rglob("*.webp")), [])
            finally:
                _ = sys.meta_path.remove(poison)

    def test_off_and_image_request_deny_before_input(self) -> None:
        with patch.object(
            timeline_module,
            "load_config",
            side_effect=AssertionError("config must not load"),
        ):
            off = run_timeline(
                "fixture",
                runtime_policy=RuntimePolicy("off"),
            )
            image = run_timeline(
                "fixture",
                images="generate",
                runtime_policy=RuntimePolicy("codex"),
            )
        self.assertEqual(off["status"], "policy_denied")
        self.assertEqual(image["status"], "unavailable")


if __name__ == "__main__":
    _ = unittest.main()
