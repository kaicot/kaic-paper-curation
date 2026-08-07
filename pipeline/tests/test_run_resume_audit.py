"""Todo 24 — partial-safe resume audit tests (RED first).

A two-stage run fails in stage 2; the audit shows the failed run preserved
stage-1 outputs and call counts, and resume reruns only the incomplete
stage, finishing SUCCEEDED with stage-1 hashes unchanged.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.lib.generation_cache import CacheIdentity, CacheSuccess, GenerationCache
from pipeline.lib.run_state import RunRequest, RunStateStore, RunStatus
from pipeline.runtime_policy import RuntimePolicy
from pipeline.update_geometry_orchestration import policy_digest

TOPIC = "fixture"


def make_identity(stage: str, source: bytes) -> CacheIdentity:
    return CacheIdentity(
        runtime_mode="codex",
        capability="codex_generation",
        role="terra",
        model="gpt-5.6-terra",
        reasoning_effort="xhigh",
        cli_version="0.146.0",
        signed_binary_sha256="0" * 64,
        attestation_sha256="0" * 64,
        contract_sha256="0" * 64,
        policy_version="runtime-v2/codex-v1",
        policy_sha256="0" * 64,
        prompt_version="v1",
        prompt_sha256=hashlib.sha256(f"prompt-{stage}".encode()).hexdigest(),
        schema_version="review-schema-v1",
        schema_sha256="0" * 64,
        source_sha256=hashlib.sha256(source).hexdigest(),
        task_id=f"safe-{stage}",
    )


class RunResumeAuditTests(unittest.TestCase):
    def test_failed_stage_preserves_earlier_outputs_and_resume_reruns_only_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_dir = root / "state"
            cache = GenerationCache(root / "cache")
            store = RunStateStore(state_dir, workspace_root=root)
            policy = RuntimePolicy("codex")
            source = b"stable input text"
            counting = {"stage1": 0, "stage2": 0}

            # First run: stage 1 succeeds, stage 2 crashes.
            first_request = RunRequest.create(TOPIC, "safe-run", policy_digest(policy))
            lease = store.acquire(first_request)
            try:
                identity1 = make_identity("stage1", source)
                result1 = cache.get_or_generate(
                    identity1,
                    lambda: (counting.__setitem__("stage1", counting["stage1"] + 1),
                             CacheSuccess(result={"stage": "review", "body": "리뷰 본문"}))[1],
                )
                review_out = root / "review.md"
                review_out.write_text(result1["body"], encoding="utf-8")
                review_hash = hashlib.sha256(review_out.read_bytes()).hexdigest()
                lease.set_stage_inputs("review", {"text_sha256": hashlib.sha256(source).hexdigest()})
                lease.complete_stage("review", artifacts=[{"path": "review.md", "sha256": review_hash}])

                identity2 = make_identity("stage2", source)
                with self.assertRaises(RuntimeError):
                    cache.get_or_generate(
                        identity2,
                        lambda: (counting.__setitem__("stage2", counting["stage2"] + 1),
                                 (_ for _ in ()).throw(RuntimeError("render crash")))[1],
                    )
                lease.finish(RunStatus.FAILED)
            finally:
                lease.release()

            # Audit: stage-1 output unchanged, counts frozen, run recorded FAILED.
            self.assertEqual(hashlib.sha256(review_out.read_bytes()).hexdigest(), review_hash)
            self.assertEqual(counting, {"stage1": 1, "stage2": 1})
            self.assertEqual(store.audit_topic(TOPIC), RunStatus.FAILED)

            # Resume: rerun only the incomplete stage; stage-1 cache is a hit.
            lease = store.acquire(
                RunRequest.create(TOPIC, "safe-run", policy_digest(policy), run_id=first_request.run_id)
            )
            try:
                result2 = cache.get_or_generate(
                    identity2,
                    lambda: (counting.__setitem__("stage2", counting["stage2"] + 1),
                             CacheSuccess(result={"stage": "render", "body": "rendered"}))[1],
                )
                html_out = root / "index.html"
                html_out.write_text(result2["body"], encoding="utf-8")
                html_hash = hashlib.sha256(html_out.read_bytes()).hexdigest()
                lease.set_stage_inputs("render", {"review_sha256": review_hash})
                lease.complete_stage("render", artifacts=[{"path": "index.html", "sha256": html_hash}])
                lease.finish(RunStatus.SUCCEEDED)
            finally:
                lease.release()
            self.assertEqual(counting, {"stage1": 1, "stage2": 2}, "resume must not rerun stage 1")
            self.assertTrue(html_out.is_file())
            # Successful resume removes the topic marker; no unfinished run remains.
            self.assertIsNone(store.audit_topic(TOPIC))

    def test_complete_run_is_audited_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_dir = root / "state"
            store = RunStateStore(state_dir, workspace_root=root)
            policy = RuntimePolicy("codex")
            lease = store.acquire(RunRequest.create(TOPIC, "safe-run", policy_digest(policy)))
            try:
                review_out = root / "review.md"
                review_out.write_text("리뷰", encoding="utf-8")
                review_hash = hashlib.sha256(review_out.read_bytes()).hexdigest()
                lease.set_stage_inputs("review", {"text_sha256": "a" * 64})
                lease.complete_stage("review", artifacts=[{"path": "review.md", "sha256": review_hash}])
                lease.finish(RunStatus.SUCCEEDED)
            finally:
                lease.release()
            # Successful runs remove the topic marker; audit reports no unfinished run.
            self.assertIsNone(store.audit_topic(TOPIC))


if __name__ == "__main__":
    unittest.main()
