"""Generation cache identity and publication contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.lib.generation_cache import (
    CacheFailure,
    CacheIdentity,
    CacheSuccess,
    GenerationCache,
    GenerationCacheError,
)
from pipeline.providers.codex_gateway import CodexGateway, GatewayPaths
from pipeline.runtime_policy import RuntimePolicy
from pipeline.tests.test_codex_exec import FakeRunner


def _identity(**changes: str) -> CacheIdentity:
    """Build a complete, distinct-value identity fixture."""
    values = {
        "runtime_mode": "codex",
        "capability": "generation",
        "role": "long_form",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "xhigh",
        "cli_version": "0.146.0",
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


class GenerationCacheTests(unittest.TestCase):
    def test_identity_is_derived_from_requalified_gateway_attestation(self) -> None:
        # Given: one real gateway contract backed by a counted fake Codex child.
        with tempfile.TemporaryDirectory(prefix="generation-cache-gateway-") as directory:
            root = Path(directory)
            executable = root / "codex-fake.exe"
            _ = executable.write_bytes(b"signed-fake-codex-v1")
            runner = FakeRunner()
            with patch.dict(os.environ, {"PAPER_CURATION_TESTING": "1"}):
                gateway = CodexGateway.for_testing(
                    GatewayPaths(ROOT, executable, root / "codex-resolved.json", True),
                    runner,
                )
                _ = gateway.requalify(accept=True)
                first = CacheIdentity.from_gateway(
                    runtime_policy=RuntimePolicy("codex"),
                    gateway=gateway,
                    role="long_form",
                    prompt_version="review-v1",
                    prompt="fixture prompt",
                    schema_version="review-v1",
                    schema={"properties": {"answer": {"type": "string"}}, "type": "object"},
                    source=b"paper source",
                    task_id="review:001",
                )

                # When: the same CLI version is requalified with a different binary.
                _ = executable.write_bytes(b"signed-fake-codex-v2")
                _ = gateway.requalify(accept=True)
                second = CacheIdentity.from_gateway(
                    runtime_policy=RuntimePolicy("codex"),
                    gateway=gateway,
                    role="long_form",
                    prompt_version="review-v1",
                    prompt="fixture prompt",
                    schema_version="review-v1",
                    schema={"properties": {"answer": {"type": "string"}}, "type": "object"},
                    source=b"paper source",
                    task_id="review:001",
                )
                gateway.contract["probe_version"] = 2
                _ = gateway.requalify(accept=True)
                third = CacheIdentity.from_gateway(
                    runtime_policy=RuntimePolicy("codex"),
                    gateway=gateway,
                    role="long_form",
                    prompt_version="review-v1",
                    prompt="fixture prompt",
                    schema_version="review-v1",
                    schema={"properties": {"answer": {"type": "string"}}, "type": "object"},
                    source=b"paper source",
                    task_id="review:001",
                )

                # Then: binary and contract requalification independently invalidate the cache.
                self.assertEqual(first.cli_version, second.cli_version)
                self.assertNotEqual(first.signed_binary_sha256, second.signed_binary_sha256)
                self.assertNotEqual(first.attestation_sha256, second.attestation_sha256)
                self.assertNotEqual(first.digest, second.digest)
                self.assertEqual(second.signed_binary_sha256, third.signed_binary_sha256)
                self.assertNotEqual(second.contract_sha256, third.contract_sha256)
                self.assertNotEqual(second.attestation_sha256, third.attestation_sha256)
                self.assertNotEqual(second.digest, third.digest)
                self.assertEqual(first.capability, "codex_generation")
                with self.assertRaisesRegex(GenerationCacheError, "policy-denied"):
                    _ = CacheIdentity.from_gateway(
                        runtime_policy=RuntimePolicy("off"),
                        gateway=gateway,
                        role="long_form",
                        prompt_version="review-v1",
                        prompt="fixture prompt",
                        schema_version="review-v1",
                        schema={"type": "object"},
                        source=b"paper source",
                        task_id="review:001",
                    )

    def test_reuses_only_an_exact_provider_policy_source_identity(self) -> None:
        # Given: one complete attested Codex identity and a counted fake caller.
        with tempfile.TemporaryDirectory(prefix="generation-cache-") as directory:
            cache = GenerationCache(Path(directory))
            calls = 0

            def call() -> CacheSuccess:
                nonlocal calls
                calls += 1
                return CacheSuccess({"answer": "fixture"})

            # When: the same real cache surface is called twice.
            first = cache.get_or_generate(_identity(), call)
            second = cache.get_or_generate(_identity(), call)

            # Then: its digest-identical result is reused without a second fake call.
            self.assertEqual(first, {"answer": "fixture"})
            self.assertEqual(second, first)
            self.assertEqual(calls, 1)
            self.assertEqual(
                hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                hashlib.sha256(json.dumps(second, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            )

    def test_invalidates_independently_for_every_identity_field(self) -> None:
        # Given: each identity field has a deliberately different replacement value.
        replacements = {
            "runtime_mode": "off",
            "capability": "summary",
            "role": "short_form",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "cli_version": "0.147.0",
            "signed_binary_sha256": "8" * 64,
            "attestation_sha256": "9" * 64,
            "contract_sha256": "a" * 64,
            "policy_version": "2",
            "policy_sha256": "b" * 64,
            "prompt_version": "review-v2",
            "prompt_sha256": "c" * 64,
            "schema_version": "review-v2",
            "schema_sha256": "d" * 64,
            "source_sha256": "e" * 64,
            "task_id": "review:002",
        }
        with tempfile.TemporaryDirectory(prefix="generation-cache-") as directory:
            cache = GenerationCache(Path(directory))
            calls = 0

            def call() -> CacheSuccess:
                nonlocal calls
                calls += 1
                return CacheSuccess({"call": calls})

            # When: every individual contract component changes in isolation.
            baseline = cache.get_or_generate(_identity(), call)
            outcomes = [cache.get_or_generate(_identity(**{field: value}), call) for field, value in replacements.items()]

            # Then: every change forces a separate generation, including requalification drifts.
            self.assertEqual(baseline, {"call": 1})
            self.assertEqual(outcomes, [{"call": index} for index in range(2, len(replacements) + 2)])
            self.assertEqual(calls, len(replacements) + 1)

    def test_denied_cancelled_failed_and_partial_outcomes_publish_no_success(self) -> None:
        # Given: a cache whose producer reports each non-success terminal outcome.
        with tempfile.TemporaryDirectory(prefix="generation-cache-") as directory:
            cache = GenerationCache(Path(directory))
            for status in ("denied", "cancelled", "failed", "partial"):
                identity = _identity(task_id=f"review:{status}")

                # When: the producer returns that non-success status.
                with self.assertRaisesRegex(GenerationCacheError, status):
                    _ = cache.get_or_generate(identity, lambda status=status: CacheFailure(status))

                # Then: no success envelope exists for it.
                self.assertIsNone(cache.load(identity))
                self.assertFalse((Path(directory) / f"{identity.digest}.json").exists())

    def test_killed_producer_and_legacy_or_corrupt_entries_are_never_hits(self) -> None:
        # Given: a legacy entry and a child producer that exits after its temp write.
        with tempfile.TemporaryDirectory(prefix="generation-cache-process-") as directory:
            root = Path(directory)
            identity = _identity()
            legacy = root / ("a" * 24 + ".json")
            _ = legacy.write_text('{"result":{"answer":"legacy"}}', encoding="utf-8")
            script = root / "producer.py"
            _ = script.write_text(
                "\n".join(
                    (
                        "import os, sys",
                        f"sys.path.insert(0, {str(ROOT)!r})",
                        "from pathlib import Path",
                        "from pipeline.lib.generation_cache import CacheIdentity, CacheSuccess, GenerationCache",
                        f"values = {identity.as_json()!r}",
                        "identity = CacheIdentity(**values)",
                        "def terminate(_temporary): os._exit(86)",
                        f"GenerationCache(Path({str(root)!r}), before_publish=terminate).get_or_generate(identity, lambda: CacheSuccess({{'answer':'partial'}}))",
                    )
                ),
                encoding="utf-8",
            )

            # When: the real child process terminates before atomic publication.
            child = subprocess.run([sys.executable, str(script)], capture_output=True, check=False)
            partial_directories = list(root.glob(f".{identity.digest}.*"))
            corrupt = root / f"{identity.digest}.json"
            _ = corrupt.write_text('{"result":{"answer":NaN}}', encoding="utf-8")
            calls = 0

            def restart() -> CacheSuccess:
                nonlocal calls
                calls += 1
                return CacheSuccess({"answer": "recomputed"})

            result = GenerationCache(root).get_or_generate(identity, restart)

            # Then: neither partial, legacy, nor non-standard JSON is reused or deleted.
            self.assertEqual(child.returncode, 86)
            self.assertEqual(len(partial_directories), 1)
            self.assertTrue(partial_directories[0].is_dir())
            self.assertEqual(result, {"answer": "recomputed"})
            self.assertEqual(calls, 1)
            self.assertTrue(legacy.is_file())

            # And: a hash-consistent scalar envelope is incompatible and recomputed.
            scalar = "incompatible"
            scalar_payload = {
                "identity": identity.as_json(),
                "identity_sha256": identity.digest,
                "result": scalar,
                "result_sha256": hashlib.sha256(
                    (json.dumps(scalar, sort_keys=True, separators=(",", ":")) + "\n").encode()
                ).hexdigest(),
                "schema": "generation-cache-v1",
                "schema_version": 1,
                "state": "succeeded",
            }
            _ = corrupt.write_text(json.dumps(scalar_payload), encoding="utf-8")
            scalar_calls = 0

            def replace_scalar() -> CacheSuccess:
                nonlocal scalar_calls
                scalar_calls += 1
                return CacheSuccess({"answer": "object"})

            self.assertEqual(GenerationCache(root).get_or_generate(identity, replace_scalar), {"answer": "object"})
            self.assertEqual(scalar_calls, 1)

            # And: duplicate members and non-canonical bytes are corrupt cache misses.
            duplicate = corrupt.read_text(encoding="utf-8").replace(
                '"state":"succeeded"',
                '"state":"failed","state":"succeeded"',
            )
            _ = corrupt.write_text(duplicate, encoding="utf-8")
            duplicate_calls = 0

            def replace_duplicate() -> CacheSuccess:
                nonlocal duplicate_calls
                duplicate_calls += 1
                return CacheSuccess({"answer": "deduplicated"})

            self.assertEqual(
                GenerationCache(root).get_or_generate(identity, replace_duplicate),
                {"answer": "deduplicated"},
            )
            self.assertEqual(duplicate_calls, 1)
            valid_payload = cast(object, json.loads(corrupt.read_text(encoding="utf-8")))
            _ = corrupt.write_text(json.dumps(valid_payload, indent=2), encoding="utf-8")
            canonical_calls = 0

            def replace_noncanonical() -> CacheSuccess:
                nonlocal canonical_calls
                canonical_calls += 1
                return CacheSuccess({"answer": "canonical"})

            self.assertEqual(
                GenerationCache(root).get_or_generate(identity, replace_noncanonical),
                {"answer": "canonical"},
            )
            self.assertEqual(canonical_calls, 1)

    def test_post_validation_mutation_and_nonfinite_result_cannot_publish(self) -> None:
        # Given: callbacks/results that would create invalid JSON success envelopes.
        with tempfile.TemporaryDirectory(prefix="generation-cache-invalid-") as directory:
            root = Path(directory)
            identity = _identity()

            def mutate(temporary: Path) -> None:
                _ = temporary.write_text(
                    '{"state":"succeeded","result":{"answer":"tampered"}}',
                    encoding="utf-8",
                )

            # When/Then: mutation after initial validation is checked again before replace.
            with self.assertRaisesRegex(GenerationCacheError, "envelope-invalid"):
                _ = GenerationCache(root, before_publish=mutate).get_or_generate(
                    identity,
                    lambda: CacheSuccess({"answer": "original"}),
                )
            self.assertFalse((root / f"{identity.digest}.json").exists())

            # And: Python's non-standard NaN value is rejected as corrupt, not cached.
            with self.assertRaisesRegex(GenerationCacheError, "result-invalid"):
                _ = GenerationCache(root).get_or_generate(
                    identity,
                    lambda: CacheSuccess({"answer": float("nan")}),
                )
            self.assertFalse((root / f"{identity.digest}.json").exists())

    def test_concurrent_readers_never_observe_a_partial_envelope(self) -> None:
        # Given: a writer paused after a complete temporary envelope but before atomic replace.
        with tempfile.TemporaryDirectory(prefix="generation-cache-") as directory:
            ready = threading.Event()
            release = threading.Event()

            def pause_before_publish(temporary: Path) -> None:
                self.assertTrue(temporary.is_file())
                ready.set()
                self.assertTrue(release.wait(timeout=5))

            identity = _identity()
            writer_cache = GenerationCache(Path(directory), before_publish=pause_before_publish)
            writer_result: list[object] = []

            def publish() -> None:
                writer_result.append(writer_cache.get_or_generate(identity, lambda: CacheSuccess({"answer": "complete"})))

            writer = threading.Thread(target=publish)
            writer.start()
            self.assertTrue(ready.wait(timeout=5))

            # When: readers inspect the final cache path while the writer has only a temp file.
            observations = [GenerationCache(Path(directory)).load(identity) for _ in range(20)]
            release.set()
            writer.join(timeout=5)

            # Then: they see only cache misses before publication and one complete envelope after it.
            self.assertFalse(writer.is_alive())
            self.assertEqual(observations, [None] * 20)
            self.assertEqual(writer_result, [{"answer": "complete"}])
            self.assertEqual(GenerationCache(Path(directory)).load(identity), {"answer": "complete"})


if __name__ == "__main__":
    _ = unittest.main()
