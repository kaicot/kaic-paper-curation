"""Retained paper connections through saved-auth Codex only."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast, final
from unittest.mock import patch

import numpy as np

from pipeline.lib.generation_cache import CacheIdentity, GenerationCache
from pipeline.providers.codex_gateway import CodexGatewayError
from pipeline.runtime_policy import RuntimePolicy
from pipeline.schemas.codex_schema import JsonObject


connections_stage = importlib.import_module("pipeline.extract_insights")
topic_modeling = importlib.import_module("pipeline.topic_modeling")
connection_store = importlib.import_module("pipeline.lib.connections")
run_connections = cast(
    Callable[..., JsonObject],
    getattr(connections_stage, "_run_insights"),
)
connections_main = cast(
    Callable[[], int],
    getattr(connections_stage, "main"),
)
extract_connections = cast(
    Callable[..., JsonObject],
    getattr(connections_stage, "extract_paper_connections"),
)
ConnectionGenerationError = cast(
    type[RuntimeError],
    getattr(connections_stage, "ConnectionGenerationError"),
)
topic_codex_factory = cast(
    Callable[..., object],
    getattr(topic_modeling, "TopicCodex"),
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


class ConnectionGenerator(Protocol):
    pass


@final
class FakeGateway:
    def __init__(
        self,
        *,
        invalid: bool = False,
        error_code: str | None = None,
    ) -> None:
        self.invalid = invalid
        self.error_code = error_code
        self.calls: list[str] = []

    def generate_json(self, role: str, prompt: str, schema: JsonObject) -> JsonObject:
        _ = schema
        self.calls.append(role)
        if self.error_code is not None:
            raise CodexGatewayError(self.error_code, "sanitized failure")
        if self.invalid:
            return {"decisions": []}
        sources = cast(
            list[str],
            re.findall(r"(?m)^\[(\d+)\].*candidates:", prompt),
        )
        return cast(
            JsonObject,
            cast(
                object,
                {
                    "decisions": [
                        {"source": source, "connections": []}
                        for source in sources
                    ]
                },
            ),
        )


def _papers() -> list[JsonObject]:
    return [
        cast(
            JsonObject,
            cast(
                object,
                {
                    "classifications": {
                        "fixture": {
                            "all_categories": ["Agents"],
                            "primary_category": "Agents",
                        }
                    },
                    "essence": f"논문 {number}의 핵심 기여",
                    "score": 5 - number,
                    "slug": f"{number:03d}_Paper",
                    "title": f"Paper {number}",
                    "topics": ["fixture"],
                },
            ),
        )
        for number in range(1, 4)
    ]


def _candidate_map() -> dict[str, list[tuple[str, float]]]:
    return {
        "001_Paper": [("002_Paper", 0.9), ("003_Paper", 0.8)],
        "002_Paper": [("001_Paper", 0.9), ("003_Paper", 0.7)],
        "003_Paper": [("001_Paper", 0.8), ("002_Paper", 0.7)],
    }


@final
class CodexConnectionsTests(unittest.TestCase):
    def _patch_local_pipeline(
        self,
        papers_dir: Path,
        topic_dir: Path,
        candidates: dict[str, list[tuple[str, float]]],
    ):
        papers = _papers()

        def fake_candidates(
            embeddings: np.ndarray,
            slugs: list[str],
            top_k: int = 5,
        ) -> dict[str, list[tuple[str, float]]]:
            self.assertEqual(embeddings.shape, (3, 3))
            self.assertEqual(slugs, ["001_Paper", "002_Paper", "003_Paper"])
            self.assertEqual(top_k, 25)
            return candidates

        return (
            patch.object(connections_stage, "PAPERS_DIR", str(papers_dir)),
            patch.object(connections_stage, "get_topic_dir", return_value=topic_dir),
            patch.object(connection_store, "PAPERS_DIR", str(papers_dir)),
            patch.object(
                connection_store,
                "GLOBAL_CONN_PATH",
                str(papers_dir / "_global_connections.json"),
            ),
            patch.object(
                topic_modeling,
                "extract_originalities",
                return_value={
                    cast(str, paper["slug"]): f"originality {index}"
                    for index, paper in enumerate(papers)
                },
            ),
            patch.object(
                topic_modeling,
                "compute_embeddings",
                return_value=(np.eye(3, dtype=np.float32), [
                    "001_Paper", "002_Paper", "003_Paper"
                ]),
            ),
            patch.object(
                topic_modeling,
                "compute_related_candidates",
                side_effect=fake_candidates,
            ),
            patch.dict(
                "os.environ",
                {"CONN_FULL_REBUILD": "1", "CONN_RENDER_NEIGHBORS": "0"},
            ),
        )

    def test_long_form_cache_remap_and_default_no_insights(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-connections-") as directory:
            root = Path(directory)
            papers_dir = root / "papers"
            topic_dir = root / "fixture"
            papers_dir.mkdir()
            topic_dir.mkdir()
            _ = (papers_dir / "_papers_index.json").write_text(
                json.dumps(_papers(), ensure_ascii=False),
                encoding="utf-8",
            )
            gateway = FakeGateway()
            generator = cast(
                ConnectionGenerator,
                topic_codex_factory(
                    RuntimePolicy("codex"),
                    gateway,
                    GenerationCache(topic_dir / ".llm_cache"),
                    _identity_factory,
                ),
            )
            patches = self._patch_local_pipeline(
                papers_dir,
                topic_dir,
                _candidate_map(),
            )
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patches[6],
                patches[7],
            ):
                first = run_connections(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                    generator=generator,
                )
                first_bytes = (topic_dir / "_paper_connections.json").read_bytes()
                second = run_connections(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                    generator=generator,
                )
            self.assertEqual(first["status"], "ok")
            self.assertEqual(second["status"], "ok")
            self.assertEqual(gateway.calls, ["long_form"])
            self.assertEqual(
                (topic_dir / "_paper_connections.json").read_bytes(),
                first_bytes,
            )
            self.assertFalse((topic_dir / "_insights.json").exists())
            self.assertFalse(hasattr(connections_stage, "extract_cross_category_insights"))

    def test_codex_failure_has_no_fallback_and_preserves_prior_connections(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-connections-fail-") as directory:
            root = Path(directory)
            papers_dir = root / "papers"
            topic_dir = root / "fixture"
            papers_dir.mkdir()
            topic_dir.mkdir()
            _ = (papers_dir / "_papers_index.json").write_text(
                json.dumps(_papers(), ensure_ascii=False),
                encoding="utf-8",
            )
            connection_path = topic_dir / "_paper_connections.json"
            prior = b'{"001_Paper":[{"slug":"002_Paper","relation":"extension"}]}\n'
            _ = connection_path.write_bytes(prior)
            gateway = FakeGateway(invalid=True)
            failure_cache = topic_dir / "failure-cache"
            generator = cast(
                ConnectionGenerator,
                topic_codex_factory(
                    RuntimePolicy("codex"),
                    gateway,
                    GenerationCache(failure_cache),
                    _identity_factory,
                ),
            )
            patches = self._patch_local_pipeline(
                papers_dir,
                topic_dir,
                _candidate_map(),
            )
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patches[6],
                patches[7],
            ):
                result = run_connections(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                    generator=generator,
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "generation-incomplete")
            self.assertEqual(gateway.calls, ["long_form"])
            self.assertEqual(connection_path.read_bytes(), prior)
            self.assertEqual(list(failure_cache.glob("*.json")), [])
            self.assertFalse((topic_dir / "_insights.json").exists())

            for code in ("process-failed", "auth-status"):
                typed_gateway = FakeGateway(error_code=code)
                typed_generator = cast(
                    ConnectionGenerator,
                    topic_codex_factory(
                        RuntimePolicy("codex"),
                        typed_gateway,
                        GenerationCache(topic_dir / f"{code}-cache"),
                        _identity_factory,
                    ),
                )
                typed_patches = self._patch_local_pipeline(
                    papers_dir,
                    topic_dir,
                    _candidate_map(),
                )
                with (
                    typed_patches[0],
                    typed_patches[1],
                    typed_patches[2],
                    typed_patches[3],
                    typed_patches[4],
                    typed_patches[5],
                    typed_patches[6],
                    typed_patches[7],
                ):
                    typed_result = run_connections(
                        "fixture",
                        runtime_policy=RuntimePolicy("codex"),
                        generator=typed_generator,
                    )
                self.assertEqual(typed_result["status"], "failed")
                self.assertEqual(connection_path.read_bytes(), prior)
                self.assertEqual(typed_gateway.calls, ["long_form"])

    def test_off_and_explicit_insights_deny_before_data_or_generator(self) -> None:
        prior = b'{"preserved":true}\n'
        with tempfile.TemporaryDirectory(prefix="connections-policy-") as directory:
            path = Path(directory) / "_paper_connections.json"
            _ = path.write_bytes(prior)
            with patch.object(
                connections_stage,
                "load_topic_data",
                side_effect=AssertionError("data must not load"),
            ):
                off = run_connections(
                    "fixture",
                    runtime_policy=RuntimePolicy("off"),
                )
                insights = run_connections(
                    "fixture",
                    insights_only=True,
                    connections_only=False,
                    runtime_policy=RuntimePolicy("codex"),
                )
            self.assertEqual(off["status"], "policy_denied")
            self.assertEqual(insights["status"], "policy_denied")
            self.assertEqual(path.read_bytes(), prior)

    def test_legacy_all_selector_runs_connections_without_cross_insights(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(topic: str, **kwargs: object) -> JsonObject:
            _ = topic
            captured.update(kwargs)
            return {
                "status": "ok",
                "completed_slugs": [],
                "dirty_slugs": [],
            }

        with (
            patch.object(
                connections_stage,
                "resolve_runtime_policy",
                return_value=RuntimePolicy("codex"),
            ),
            patch.object(connections_stage, "_run_insights", side_effect=fake_run),
            patch.object(sys, "argv", ["extract_insights.py", "--only", "all"]),
        ):
            self.assertEqual(connections_main(), 0)
        self.assertEqual(captured["insights_only"], False)
        self.assertEqual(captured["connections_only"], True)


if __name__ == "__main__":
    _ = unittest.main()
