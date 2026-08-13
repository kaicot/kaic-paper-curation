"""Transactional run-state and interprocess lock contract tests."""

from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import IO, cast, override
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.lib import run_state
from pipeline.lib.run_state import (
    ProcessIdentity,
    ResumeRequiredError,
    RunRequest,
    RunStateError,
    RunStateStore,
    RunStatus,
    TopicBusyError,
)


class RunStateTests(unittest.TestCase):
    """Exercise every durable transition against an isolated workspace."""

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(prefix="run-state-")
        self.workspace: Path = Path(self.temporary.name) / "workspace"
        self.state_root: Path = self.workspace / "pipeline" / "_state"
        self.store: RunStateStore = RunStateStore(self.state_root, workspace_root=self.workspace)

    @override
    def setUp(self) -> None:
        self.addCleanup(self.temporary.cleanup)

    def request(
        self,
        *,
        topic: str = "ai4s",
        run_id: str = "run-001",
        policy: str = "1" * 64,
    ) -> RunRequest:
        return RunRequest.create(topic, "curate", policy, run_id=run_id)

    def artifact(self, relative: str, payload: bytes) -> dict[str, str]:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(payload)
        return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}

    def manifest(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.store.root).as_posix(): path.read_bytes()
            for directory in (self.store.runs, self.store.topics, self.store.quarantine)
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

    def stage(self, run_id: str, name: str) -> dict[str, run_state.JsonValue]:
        stages = self.store.load(run_id)["stages"]
        if not isinstance(stages, dict):
            self.fail("validated record lost its stages object")
        stage = stages.get(name)
        if not isinstance(stage, dict):
            self.fail(f"validated stage is missing: {name}")
        return stage

    def test_claim_order_and_terminal_marker_transitions(self) -> None:
        request = self.request()
        writes: list[Path] = []
        real_atomic = run_state.atomic_state_json

        def recording_atomic(path: Path, value: run_state.JsonObject) -> None:
            if path == self.store.marker_path(request.topic):
                self.assertTrue(self.store.run_path(request.run_id).is_file())
            writes.append(path)
            real_atomic(path, value)

        with patch("pipeline.lib.run_state.atomic_state_json", side_effect=recording_atomic):
            lease = self.store.acquire(request)
        self.assertEqual(writes[:2], [self.store.run_path(request.run_id), self.store.marker_path(request.topic)])
        record = self.store.load(request.run_id)
        for field in ("host", "pid", "process_start", "started_at", "heartbeat"):
            self.assertIn(field, record)
        self.assertTrue(self.store.marker_path(request.topic).is_file())
        before_invalid = self.store.run_path(request.run_id).read_bytes()
        with self.assertRaisesRegex(RunStateError, "invalid-terminal-status"):
            lease.finish(RunStatus.RUNNING)
        self.assertEqual(self.store.run_path(request.run_id).read_bytes(), before_invalid)
        lease.finish(RunStatus.FAILED)
        lease.release()
        self.assertTrue(self.store.marker_path(request.topic).is_file())
        self.assertEqual(self.store.load(request.run_id)["status"], "failed")

        before_contender = self.manifest()
        with self.assertRaises(ResumeRequiredError) as caught:
            _ = self.store.acquire(self.request(run_id="run-002"))
        resume_error = caught.exception
        self.assertEqual(resume_error.exit_code, 75)
        self.assertEqual(resume_error.http_status, 409)
        self.assertEqual(self.manifest(), before_contender)

        resumed = self.store.acquire(request)
        real_replace = run_state.durable_replace
        durable_success: list[Path] = []

        def recording_replace(source: Path, destination: Path) -> None:
            if destination == self.store.run_path(request.run_id):
                self.assertTrue(self.store.marker_path(request.topic).is_file())
                durable_success.append(destination)
            real_replace(source, destination)

        with patch("pipeline.lib.run_state.durable_replace", side_effect=recording_replace):
            resumed.finish(RunStatus.SUCCEEDED)
        resumed.release()
        self.assertEqual(durable_success, [self.store.run_path(request.run_id)])
        self.assertFalse(self.store.marker_path(request.topic).exists())
        self.assertEqual(self.store.load(request.run_id)["status"], "succeeded")

        dry = self.request(topic="dry-topic", run_id="dry-001")
        dry_lease = self.store.acquire(dry)
        dry_lease.finish(RunStatus.DRY_RUN)
        dry_lease.release()
        self.assertEqual(self.store.load(dry.run_id)["status"], "dry_run")
        self.assertTrue(self.store.marker_path(dry.topic).is_file())

        unfinished = self.request(topic="unfinished", run_id="unfinished-run")
        unfinished_lease = self.store.acquire(unfinished)
        unfinished_lease.release()
        self.assertEqual(self.store.load(unfinished.run_id)["status"], "interrupted")
        self.assertTrue(self.store.marker_path(unfinished.topic).is_file())

    def test_record_before_marker_failure_recovers_without_overwrite(self) -> None:
        request = self.request(topic="orphan", run_id="orphan-run")
        real_atomic = run_state.atomic_state_json

        def fail_marker(path: Path, value: run_state.JsonObject) -> None:
            if path == self.store.marker_path(request.topic):
                raise OSError("simulated marker publication failure")
            real_atomic(path, value)

        with patch("pipeline.lib.run_state.atomic_state_json", side_effect=fail_marker):
            with self.assertRaisesRegex(OSError, "marker publication"):
                _ = self.store.acquire(request)
        orphan = self.store.load(request.run_id)
        self.assertEqual(orphan["status"], "running")
        self.assertFalse(self.store.marker_path(request.topic).exists())
        recovered_store = RunStateStore(
            self.state_root,
            workspace_root=self.workspace,
            process_probe=lambda _host, _pid: ProcessIdentity(True, "pid-reused"),
        )
        recovered = recovered_store.acquire(request)
        self.assertEqual(recovered.record["status"], "running")
        recovered.finish(RunStatus.FAILED)
        recovered.release()

    def test_stale_marker_after_durable_success_allows_new_run(self) -> None:
        completed = self.request(topic="stale-success", run_id="completed-run")
        lease = self.store.acquire(completed)
        with patch.object(self.store, "remove_marker_after_success", return_value=None):
            lease.finish(RunStatus.SUCCEEDED)
        lease.release()
        self.assertEqual(self.store.load(completed.run_id)["status"], "succeeded")
        self.assertTrue(self.store.marker_path(completed.topic).is_file())

        replacement = self.request(topic=completed.topic, run_id="replacement-run")
        next_lease = self.store.acquire(replacement)
        self.assertEqual(self.store.load(replacement.run_id)["status"], "running")
        next_lease.finish(RunStatus.FAILED)
        next_lease.release()

    def test_same_topic_busy_child_kill_audit_and_same_run_resume(self) -> None:
        request = self.request(run_id="child-run")
        artifact = self.artifact("docs/complete.txt", b"complete")
        script = Path(self.temporary.name) / "owner.py"
        _ = script.write_text(
            "\n".join(
                (
                    "import sys",
                    f"sys.path.insert(0, {str(ROOT)!r})",
                    "from pathlib import Path",
                    "from pipeline.lib.run_state import RunRequest, RunStateStore",
                    f"workspace=Path({str(self.workspace)!r})",
                    f"store=RunStateStore(Path({str(self.state_root)!r}), workspace_root=workspace)",
                    f"request=RunRequest.create('ai4s','curate',{'1' * 64!r},run_id='child-run')",
                    "lease=store.acquire(request)",
                    f"lease.set_stage_inputs('complete',{{'paper_sha256':{'2' * 64!r}}})",
                    f"lease.complete_stage('complete',[{artifact!r}])",
                    f"lease.set_stage_inputs('incomplete',{{'paper_sha256':{'3' * 64!r}}})",
                    "print('READY', flush=True)",
                    "command=sys.stdin.readline().strip()",
                    "lease.release() if command == 'release' else None",
                )
            ),
            encoding="utf-8",
        )
        child = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(_terminate, child)
        self.assertEqual(_readline(child.stdout, 10.0), "READY\n")
        before = self.manifest()
        with self.assertRaises(TopicBusyError) as caught:
            _ = self.store.acquire(self.request(run_id="contender"))
        busy_error = caught.exception
        self.assertEqual(busy_error.exit_code, 75)
        self.assertEqual(busy_error.http_status, 409)
        self.assertEqual(self.manifest(), before)
        self.assertIsNone(child.poll())

        child.kill()
        self.assertNotEqual(child.wait(timeout=10.0), 0)
        self.assertEqual(self.store.audit_topic(request.topic), RunStatus.INTERRUPTED)
        self.assertTrue(self.store.marker_path(request.topic).is_file())
        self.assertEqual(self.store.load(request.run_id)["status"], "interrupted")
        resumed = self.store.acquire(request)
        self.assertTrue(resumed.stage_reusable("complete", {"paper_sha256": "2" * 64}))
        self.assertFalse(resumed.stage_reusable("incomplete", {"paper_sha256": "3" * 64}))
        resumed.finish(RunStatus.FAILED)
        resumed.release()

    def test_different_topics_are_independent(self) -> None:
        first = self.store.acquire(self.request(topic="ai4s", run_id="run-a"))
        second = self.store.acquire(self.request(topic="scisci", run_id="run-b"))
        self.assertEqual(self.store.load("run-a")["status"], "running")
        self.assertEqual(self.store.load("run-b")["status"], "running")
        first.finish(RunStatus.DRY_RUN)
        second.finish(RunStatus.DRY_RUN)
        first.release()
        second.release()

    def test_corrupt_and_missing_records_become_resume_required_without_data_loss(self) -> None:
        request = self.request(run_id="corrupt-run")
        lease = self.store.acquire(request)
        lease.finish(RunStatus.FAILED)
        lease.release()
        corrupt = b'{"schema":"bad","schema":"worse"}\n'
        _ = self.store.run_path(request.run_id).write_bytes(corrupt)

        self.assertEqual(self.store.audit_topic(request.topic), RunStatus.RESUME_REQUIRED)
        quarantined = list(self.store.quarantine.glob(f"{request.run_id}.*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), corrupt)
        self.assertEqual(self.store.load(request.run_id)["status"], "resume_required")
        self.assertTrue(self.store.marker_path(request.topic).is_file())

        resumed = self.store.acquire(request)
        self.assertEqual(resumed.record["stages"], {})
        resumed.finish(RunStatus.FAILED)
        resumed.release()
        self.store.run_path(request.run_id).unlink()
        self.assertEqual(self.store.audit_topic(request.topic), RunStatus.RESUME_REQUIRED)
        self.assertEqual(self.store.load(request.run_id)["status"], "resume_required")

    def test_corrupt_active_marker_is_quarantined_and_recovered(self) -> None:
        request = self.request(topic="marker-topic", run_id="marker-run")
        lease = self.store.acquire(request)
        lease.finish(RunStatus.FAILED)
        lease.release()
        corrupt = b'{"schema":"bad","schema":"worse"}\n'
        _ = self.store.marker_path(request.topic).write_bytes(corrupt)
        self.assertEqual(self.store.audit_topic(request.topic), RunStatus.RESUME_REQUIRED)
        quarantined = list(self.store.quarantine.glob(f"{request.topic}.marker.*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), corrupt)
        marker = cast(
            run_state.JsonObject,
            json.loads(self.store.marker_path(request.topic).read_text(encoding="utf-8")),
        )
        recovery_run = marker["run_id"]
        if not isinstance(recovery_run, str):
            self.fail("recovery marker lost its run id")
        self.assertEqual(self.store.load(recovery_run)["status"], "resume_required")
        recovery_request = RunRequest.create(
            request.topic,
            "recovery",
            "0" * 64,
            run_id=recovery_run,
        )
        recovery = self.store.acquire(recovery_request)
        artifact = self.artifact("docs/recovery.txt", b"first-recovery")
        recovery.set_stage_inputs("recovered", {"paper_sha256": "8" * 64})
        recovery.complete_stage("recovered", [artifact])
        recovery.finish(RunStatus.FAILED)
        recovery.release()

        second_corrupt = b"not-json"
        _ = self.store.marker_path(request.topic).write_bytes(second_corrupt)
        self.assertEqual(self.store.audit_topic(request.topic), RunStatus.RESUME_REQUIRED)
        second_marker = cast(
            run_state.JsonObject,
            json.loads(self.store.marker_path(request.topic).read_text(encoding="utf-8")),
        )
        self.assertNotEqual(second_marker["run_id"], recovery_run)
        self.assertEqual(self.stage(recovery_run, "recovered")["artifacts"], [artifact])

        self.store.marker_path(request.topic).unlink()
        self.store.marker_path(request.topic).mkdir()
        _ = (self.store.marker_path(request.topic) / "preserved.bin").write_bytes(b"directory-marker")
        self.assertEqual(self.store.audit_topic(request.topic), RunStatus.RESUME_REQUIRED)
        quarantined_directories = [
            path
            for path in self.store.quarantine.glob(f"{request.topic}.marker.*")
            if path.is_dir()
        ]
        self.assertEqual(len(quarantined_directories), 1)
        self.assertEqual(
            (quarantined_directories[0] / "preserved.bin").read_bytes(),
            b"directory-marker",
        )
        self.assertEqual(self.stage(recovery_run, "recovered")["artifacts"], [artifact])

    def test_process_identity_is_fail_closed_and_detects_pid_reuse(self) -> None:
        request = self.request(topic="identity", run_id="identity-run")
        lease = self.store.acquire(request)
        marker = cast(
            run_state.JsonObject,
            json.loads(self.store.marker_path(request.topic).read_text(encoding="utf-8")),
        )
        token = marker["process_start"]
        if not isinstance(token, str):
            self.fail("active marker lost its process-start token")
        lease.topic_lock.release()
        lease.closed = True

        matching = RunStateStore(
            self.state_root,
            workspace_root=self.workspace,
            process_probe=lambda _host, _pid: ProcessIdentity(True, token),
        )
        with self.assertRaises(TopicBusyError):
            _ = matching.audit_topic(request.topic)
        unknown = RunStateStore(
            self.state_root,
            workspace_root=self.workspace,
            process_probe=lambda _host, _pid: ProcessIdentity(None, None),
        )
        with self.assertRaises(TopicBusyError):
            _ = unknown.audit_topic(request.topic)
        marker["host"] = "remote-host"
        run_state.atomic_state_json(self.store.marker_path(request.topic), marker)
        with self.assertRaises(TopicBusyError):
            _ = self.store.audit_topic(request.topic)
        marker["host"] = self.store.load(request.run_id)["host"]
        run_state.atomic_state_json(self.store.marker_path(request.topic), marker)
        reused = RunStateStore(
            self.state_root,
            workspace_root=self.workspace,
            process_probe=lambda _host, _pid: ProcessIdentity(True, "different-start"),
        )
        self.assertEqual(reused.audit_topic(request.topic), RunStatus.INTERRUPTED)

    def test_stage_reuse_requires_hashes_and_invalidates_downstream(self) -> None:
        request = self.request(run_id="stage-run")
        first_artifact = self.artifact("docs/first.txt", b"first")
        second_artifact = self.artifact("docs/second.txt", b"second")
        lease = self.store.acquire(request)
        lease.set_stage_inputs("first", {"paper_sha256": "a" * 64})
        lease.complete_stage("first", [first_artifact])
        lease.set_stage_inputs("second", {"paper_sha256": "b" * 64})
        lease.complete_stage("second", [second_artifact])
        self.assertTrue(lease.stage_reusable("first", {"paper_sha256": "a" * 64}))
        self.assertTrue(lease.stage_reusable("second", {"paper_sha256": "b" * 64}))

        self.assertFalse(lease.stage_reusable("first", {"paper_sha256": "c" * 64}))
        self.assertEqual(self.stage(request.run_id, "first")["status"], "pending")
        self.assertEqual(self.stage(request.run_id, "second")["status"], "pending")
        self.assertEqual((self.workspace / first_artifact["path"]).read_bytes(), b"first")
        self.assertEqual((self.workspace / second_artifact["path"]).read_bytes(), b"second")

        lease.complete_stage("first", [first_artifact])
        _ = (self.workspace / first_artifact["path"]).write_bytes(b"drift")
        self.assertFalse(lease.stage_reusable("first", {"paper_sha256": "c" * 64}))
        self.assertEqual(self.stage(request.run_id, "first")["status"], "pending")
        lease.finish(RunStatus.FAILED)
        lease.release()

    def test_sensitive_inputs_and_unsafe_artifacts_are_rejected(self) -> None:
        lease = self.store.acquire(self.request(run_id="safe-run"))
        with self.assertRaisesRegex(RunStateError, "stage-input-invalid"):
            lease.set_stage_inputs("review", {"raw_paper_text": "a" * 64})
        lease.set_stage_inputs("review", {"paper_sha256": "a" * 64})
        for path in ("../secret.txt", r"C:\secret.txt", "/absolute.txt", r"docs\secret.txt"):
            with self.subTest(path=path), self.assertRaisesRegex(RunStateError, "artifact-invalid"):
                lease.complete_stage("review", [{"path": path, "sha256": "b" * 64}])
        serialized = self.store.run_path(lease.request.run_id).read_text(encoding="utf-8")
        for forbidden in ("prompt", "answer", "credential", "account", "raw_paper_text"):
            self.assertNotIn(forbidden, serialized)
        lease.finish(RunStatus.FAILED)
        lease.release()

    def test_policy_denied_preserves_artifacts_and_policy_change_invalidates(self) -> None:
        request = self.request(run_id="policy-run")
        artifact = self.artifact("docs/preserved.txt", b"preserved")
        lease = self.store.acquire(request)
        lease.set_stage_inputs("summary", {"paper_sha256": "d" * 64})
        lease.complete_stage("summary", [artifact])
        lease.finish(RunStatus.POLICY_DENIED)
        lease.release()
        self.assertTrue(self.store.marker_path(request.topic).is_file())
        self.assertEqual((self.workspace / artifact["path"]).read_bytes(), b"preserved")
        artifacts = self.stage(request.run_id, "summary")["artifacts"]
        if not isinstance(artifacts, list) or not artifacts or not isinstance(artifacts[0], dict):
            self.fail("validated artifact metadata is missing")
        self.assertEqual(artifacts[0]["sha256"], artifact["sha256"])

        same = self.store.acquire(request)
        self.assertTrue(same.stage_reusable("summary", {"paper_sha256": "d" * 64}))
        same.finish(RunStatus.SUCCEEDED)
        same.release()
        self.assertFalse(self.store.marker_path(request.topic).exists())

        changed_request = self.request(topic="changed", run_id="changed-run", policy="e" * 64)
        changed_artifact = self.artifact("docs/changed.txt", b"changed")
        changed = self.store.acquire(changed_request)
        changed.set_stage_inputs("summary", {"paper_sha256": "f" * 64})
        changed.complete_stage("summary", [changed_artifact])
        changed.finish(RunStatus.POLICY_DENIED)
        changed.release()
        resumed = self.store.acquire(self.request(topic="changed", run_id="changed-run", policy="0" * 64))
        self.assertFalse(resumed.stage_reusable("summary", {"paper_sha256": "f" * 64}))
        resumed.finish(RunStatus.FAILED)
        resumed.release()

        failed_request = self.request(topic="failed", run_id="failed-run")
        failed_artifact = self.artifact("docs/failed.txt", b"failed-preserved")
        failed = self.store.acquire(failed_request)
        failed.set_stage_inputs("summary", {"paper_sha256": "9" * 64})
        failed.complete_stage("summary", [failed_artifact])
        failed.finish(RunStatus.FAILED)
        failed.release()
        self.assertEqual((self.workspace / failed_artifact["path"]).read_bytes(), b"failed-preserved")
        self.assertEqual(
            self.stage(failed_request.run_id, "summary")["artifacts"],
            [failed_artifact],
        )


def _readline(stream: IO[str] | None, timeout: float) -> str:
    if stream is None:
        raise AssertionError("child stdout is unavailable")
    results: queue.Queue[str] = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: results.put(stream.readline()), daemon=True)
    reader.start()
    try:
        return results.get(timeout=timeout)
    except queue.Empty as error:
        raise AssertionError("child readiness timeout") from error


def _terminate(child: subprocess.Popen[str]) -> None:
    try:
        if child.poll() is None:
            child.kill()
            _ = child.wait(timeout=10.0)
    finally:
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


if __name__ == "__main__":
    _ = unittest.main()
