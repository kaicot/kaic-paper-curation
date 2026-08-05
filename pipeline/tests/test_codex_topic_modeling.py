"""Local clustering plus saved-auth Codex topic label/connection contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path
from collections.abc import Callable
from typing import Protocol, cast, final
from unittest.mock import patch

import numpy as np

from pipeline import prepare_local_models
from pipeline.lib import specter2_embed
from pipeline.lib.generation_cache import CacheIdentity, GenerationCache
from pipeline.lib.specter2_cache import (
    Specter2CacheUnavailable,
    manifest_sha256,
    verify_cache,
)
from pipeline.runtime_policy import RuntimePolicy
from pipeline.schemas.codex_schema import JsonObject


class TopicModule(Protocol):
    TopicSemanticError: type[ValueError]
    TopicCodex: type

    def validate_subtopic_labels(
        self, value: JsonObject, expected_ids: tuple[int, ...]
    ) -> JsonObject: ...
    def validate_category_labels(
        self, value: JsonObject, expected_ids: tuple[int, ...]
    ) -> JsonObject: ...
    def validate_connection_decisions(
        self, value: JsonObject, expected: dict[str, set[str]]
    ) -> JsonObject: ...
    def name_sub_topics(
        self,
        keywords: dict[int, list[tuple[str, float]]],
        topics: list[int],
        generator: object,
        topic: str,
        batch_size: int = 40,
    ) -> dict[int, dict[str, str]]: ...
    def generate_category_labels(
        self,
        generator: object,
        topic: str,
        prompt: str,
        source_groups: list[JsonObject],
        category_ids: list[int],
    ) -> dict[str, JsonObject]: ...
    def generate_connections_from_candidates(
        self,
        candidates: dict[str, list[tuple[str, float]]],
        papers: list[JsonObject],
        generator: object,
        topic: str,
    ) -> tuple[dict[str, list[JsonObject]], set[str]]: ...
    def compute_embeddings(
        self, originalities: dict[str, str], cache_path: str
    ) -> tuple[np.ndarray, list[str]]: ...


_topic_modeling = importlib.import_module("pipeline.topic_modeling")
_classify_papers = importlib.import_module("pipeline.classify_papers")
topic_modeling = cast(TopicModule, cast(object, _topic_modeling))
run_topic_model = cast(
    Callable[..., JsonObject],
    getattr(_topic_modeling, "_run_topic_model"),
)
run_classify = cast(
    Callable[..., JsonObject],
    getattr(_classify_papers, "_run_classify"),
)
topic_codex_factory = cast(
    Callable[..., object],
    getattr(_topic_modeling, "TopicCodex"),
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity_factory(**values: object) -> CacheIdentity:
    role = cast(str, values["role"])
    prompt = cast(str, values["prompt"])
    schema = cast(JsonObject, values["schema"])
    source = cast(bytes, values["source"])
    task_id = cast(str, values["task_id"])
    return CacheIdentity(
        runtime_mode="codex",
        capability="generation",
        role=role,
        model="gpt-5.6-luna" if role == "short_form" else "gpt-5.6-terra",
        reasoning_effort="xhigh",
        cli_version="0.146.0",
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
        task_id=task_id,
    )


@final
class FakeTopicGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, role: str, prompt: str, schema: JsonObject) -> JsonObject:
        schema_value = schema.get("$id", "")
        if not isinstance(schema_value, str):
            raise AssertionError("schema ID missing")
        schema_id = schema_value
        self.calls.append((role, schema_id))
        if schema_id.endswith("topic-subtopic-label-v1.json"):
            topic_matches = cast(list[str], re.findall(r"Topic (-?\d+)", prompt))
            topic_ids = sorted({int(value) for value in topic_matches})
            return cast(JsonObject, cast(object, {
                "topics": [
                    {
                        "topic_id": topic_id,
                        "name": f"Specific Topic {topic_id}",
                        "description": f"Topic {topic_id} has one specific academic purpose.",
                    }
                    for topic_id in topic_ids
                ]
            }))
        if schema_id.endswith("topic-category-label-v1.json"):
            category_matches = cast(
                list[str],
                re.findall(r"Category (\d+) \(", prompt),
            )
            category_ids = sorted({int(value) for value in category_matches})
            return cast(JsonObject, cast(object, {
                "categories": [
                    {
                        "category_id": category_id,
                        "name": f"Research Category {category_id}",
                        "description": f"Category {category_id} combines related research themes.",
                    }
                    for category_id in category_ids
                ]
            }))
        if schema_id.endswith("topic-connections-v1.json"):
            sources = cast(
                list[str],
                re.findall(r"(?m)^\[(\d+)\].*candidates:", prompt),
            )
            return cast(JsonObject, cast(object, {
                "decisions": [
                    {"source": source, "connections": []}
                    for source in sources
                ]
            }))
        raise AssertionError(f"unexpected schema: {schema_id}")


@final
class CodexTopicModelingTests(unittest.TestCase):
    def test_strict_semantic_validators_reject_wrong_ids_and_targets(self) -> None:
        with self.assertRaises(topic_modeling.TopicSemanticError):
            _ = topic_modeling.validate_subtopic_labels(
                {
                    "topics": [
                        {
                            "topic_id": 2,
                            "name": "Wrong Topic",
                            "description": "This description is long enough.",
                        }
                    ]
                },
                (1,),
            )
        with self.assertRaises(topic_modeling.TopicSemanticError):
            _ = topic_modeling.validate_category_labels(
                {
                    "categories": [
                        {
                            "category_id": 1,
                            "name": "Duplicate",
                            "description": "This description is long enough.",
                        },
                        {
                            "category_id": 2,
                            "name": "Duplicate",
                            "description": "This description is also long enough.",
                        },
                    ]
                },
                (1, 2),
            )
        with self.assertRaises(topic_modeling.TopicSemanticError):
            _ = topic_modeling.validate_connection_decisions(
                {
                    "decisions": [
                        {
                            "source": "001",
                            "connections": [
                                {
                                    "target": "999",
                                    "relation": "extension",
                                    "reason": "허용되지 않은 후보를 선택한 잘못된 연결이다.",
                                }
                            ],
                        }
                    ]
                },
                {"001": {"002"}},
            )

    def test_roles_cache_and_local_assignment_have_no_network_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="topic-codex-") as directory:
            gateway = FakeTopicGateway()
            generator = topic_codex_factory(
                RuntimePolicy("codex"),
                gateway,
                GenerationCache(Path(directory) / ".llm_cache"),
                _identity_factory,
            )
            keywords = {
                0: [("agent", 1.0), ("verification", 0.8)],
                1: [("protein", 1.0), ("structure", 0.8)],
                2: [("climate", 1.0), ("forecast", 0.8)],
                3: [("robot", 1.0), ("control", 0.8)],
            }
            topics = [0, 0, 1, 1, 2, 2, 3, 3]
            poison = patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("network forbidden"),
            )
            with poison:
                first = topic_modeling.name_sub_topics(
                    keywords, topics, generator, "fixture", batch_size=10
                )
                second = topic_modeling.name_sub_topics(
                    keywords, topics, generator, "fixture", batch_size=10
                )
                changed_keywords = dict(keywords)
                changed_keywords[0] = [("agentic", 1.0), ("verification", 0.8)]
                _ = topic_modeling.name_sub_topics(
                    changed_keywords,
                    topics,
                    generator,
                    "fixture",
                    batch_size=10,
                )
                _ = topic_modeling.generate_category_labels(
                    generator,
                    "fixture",
                    "Category 1 (2 sub-topics)\nCategory 2 (2 sub-topics)",
                    cast(list[JsonObject], cast(object, [
                        {"category_id": 1, "member_topic_ids": [0, 1]},
                        {"category_id": 2, "member_topic_ids": [2, 3]},
                    ])),
                    [1, 2],
                )
                connections, completed = (
                    topic_modeling.generate_connections_from_candidates(
                        {
                            "001_A": [("002_B", 0.9)],
                            "002_B": [("001_A", 0.9)],
                        },
                        cast(list[JsonObject], cast(object, [
                            {"slug": "001_A", "title": "A", "essence": "첫 번째 논문"},
                            {"slug": "002_B", "title": "B", "essence": "두 번째 논문"},
                        ])),
                        generator,
                        "fixture",
                    )
                )
            self.assertEqual(first, second)
            self.assertEqual(
                [role for role, _schema in gateway.calls],
                ["short_form", "short_form", "short_form", "long_form"],
            )
            self.assertEqual(connections, {"001_A": [], "002_B": []})
            self.assertEqual(completed, {"001_A", "002_B"})
            self.assertFalse((Path(directory) / "_search_index.json").exists())

    def test_exact_embedding_cache_hit_never_loads_model_or_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="topic-embedding-cache-") as directory:
            cache_path = Path(directory) / "_embeddings_cache.json"
            _ = cache_path.write_text(
                json.dumps(
                    {
                        "embed_model": specter2_embed.EMBED_TAG,
                        "slugs": ["001_A", "002_B"],
                        "embeddings": [[1.0, 0.0], [0.0, 1.0]],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    specter2_embed,
                    "embed_texts",
                    side_effect=AssertionError("model must not load"),
                ),
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
            ):
                embeddings, slugs = topic_modeling.compute_embeddings(
                    {"001_A": "first", "002_B": "second"},
                    str(cache_path),
                )
            self.assertEqual(slugs, ["001_A", "002_B"])
            self.assertEqual(embeddings.shape, (2, 2))

    def test_missing_cache_disables_without_mutating_classification(self) -> None:
        with tempfile.TemporaryDirectory(prefix="topic-cache-missing-") as directory:
            topic_dir = Path(directory)
            classification = topic_dir / "_new_classification.json"
            old = b'{"existing":true}\n'
            _ = classification.write_bytes(old)
            with (
                patch.object(topic_modeling, "get_topic_dir", return_value=topic_dir),
                patch.object(
                    specter2_embed,
                    "local_cache_status",
                    return_value={"available": False, "reason": "missing"},
                ),
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
            ):
                result = run_topic_model(
                    "fixture",
                    runtime_policy=RuntimePolicy("codex"),
                )
            self.assertEqual(
                result,
                {"status": "unavailable", "reason": "specter2-cache-unavailable"},
            )
            self.assertEqual(classification.read_bytes(), old)
            self.assertFalse((topic_dir / "_search_index.json").exists())

    def test_classify_missing_cache_preserves_existing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="classify-cache-missing-") as directory:
            topic_dir = Path(directory)
            classification = topic_dir / "_new_classification.json"
            old = b'{"existing":true}\n'
            _ = classification.write_bytes(old)
            with (
                patch.object(_classify_papers, "get_topic_dir", return_value=topic_dir),
                patch.object(
                    specter2_embed,
                    "local_cache_status",
                    return_value={"available": False, "reason": "missing"},
                ),
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
            ):
                result = run_classify("fixture")
            self.assertEqual(
                result,
                {"status": "unavailable", "reason": "specter2-cache-unavailable"},
            )
            self.assertEqual(classification.read_bytes(), old)

    def test_explicit_preparer_publishes_verified_idempotent_provenance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="specter2-prepare-") as directory:
            root = Path(directory)
            snapshots = root / "snapshots"
            base = snapshots / ("a" * 40)
            adapter = snapshots / ("b" * 40)
            (adapter / "proximity").mkdir(parents=True)
            base.mkdir(parents=True)
            _ = (base / "config.json").write_text("{}", encoding="utf-8")
            _ = (base / "model.bin").write_bytes(b"base")
            _ = (adapter / "proximity" / "adapter_config.json").write_text(
                "{}",
                encoding="utf-8",
            )
            _ = (adapter / "proximity" / "adapter.bin").write_bytes(b"adapter")
            calls: list[str] = []

            def fake_download(**kwargs: str) -> str:
                repo_id = kwargs["repo_id"]
                calls.append(repo_id)
                return str(
                    base
                    if repo_id == prepare_local_models.BASE_REPOSITORY
                    else adapter
                )

            cache_root = root / "cache"
            first = prepare_local_models.prepare_specter2(
                cache_root,
                snapshot_download=fake_download,
            )
            def no_download(*, repo_id: str, revision: str, cache_dir: str) -> str:
                _ = (repo_id, revision, cache_dir)
                raise AssertionError("idempotent prepare must not download")

            second = prepare_local_models.prepare_specter2(
                cache_root,
                snapshot_download=no_download,
            )
            self.assertEqual(first["manifest_sha256"], manifest_sha256(first))
            self.assertEqual(second["manifest_sha256"], first["manifest_sha256"])
            self.assertEqual(
                calls,
                [
                    prepare_local_models.BASE_REPOSITORY,
                    prepare_local_models.ADAPTER_REPOSITORY,
                ],
            )
            self.assertEqual(
                verify_cache(cache_root)["manifest_sha256"],
                first["manifest_sha256"],
            )
            manifest_bytes = (cache_root / "specter2-provenance.json").read_bytes()
            self.assertTrue(manifest_bytes.endswith(b"\n"))
            _ = (cache_root / "base" / "model.bin").write_bytes(b"tampered")
            with self.assertRaises(Specter2CacheUnavailable):
                _ = verify_cache(cache_root)
            self.assertEqual(
                (cache_root / "specter2-provenance.json").read_bytes(),
                manifest_bytes,
            )


if __name__ == "__main__":
    _ = unittest.main()
