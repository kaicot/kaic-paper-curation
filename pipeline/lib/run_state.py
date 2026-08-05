"""Transactional per-run state and per-topic interprocess exclusion."""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
import re
import socket
import tempfile
import uuid
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Callable, Final, Protocol, TypeAlias, cast, final, override


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
STATE_SCHEMA: Final = "pipeline-run-state-v1"
MARKER_SCHEMA: Final = "pipeline-topic-active-v1"
STATE_VERSION: Final = 1
_IDENTIFIER: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HASH_LABEL: Final = re.compile(r"^[a-z][a-z0-9_]{0,47}_sha256$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_STAGE_STATUSES: Final = frozenset({"pending", "succeeded"})
_TERMINAL: Final = frozenset({"dry_run", "succeeded", "failed", "interrupted", "resume_required", "policy_denied"})


class _Msvcrt(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


class _Fcntl(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Tri-state liveness plus an OS process-creation token."""

    alive: bool | None
    start_token: str | None


ProcessProbe: TypeAlias = Callable[[str, int], ProcessIdentity]


class RunStatus(StrEnum):
    """Persisted lifecycle states for one run."""

    DRY_RUN = "dry_run"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RESUME_REQUIRED = "resume_required"
    POLICY_DENIED = "policy_denied"


class RunStateError(RuntimeError):
    """A state, ownership, transition, or artifact contract was rejected."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code: str = code
        self.detail: str = detail

    @override
    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@final
class TopicBusyError(RunStateError):
    """Adapters map a live competing owner to CLI 75 or HTTP 409."""

    exit_code: Final = 75
    http_status: Final = 409

    def __init__(self, topic: str) -> None:
        super().__init__("topic-busy", f"topic already has an active owner: {topic}")


@final
class ResumeRequiredError(RunStateError):
    """A different run cannot replace a durable unfinished marker."""

    exit_code: Final = 75
    http_status: Final = 409

    def __init__(self, topic: str, run_id: str) -> None:
        super().__init__("resume-required", f"resume run {run_id} before starting topic {topic}")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Validated non-sensitive identity for one run."""

    run_id: str
    topic: str
    mode: str
    policy_digest: str

    @classmethod
    def create(
        cls,
        topic: str,
        mode: str,
        policy_digest: str,
        *,
        run_id: str | None = None,
    ) -> "RunRequest":
        request = cls(run_id or uuid.uuid4().hex, topic, mode, policy_digest)
        request.validate()
        return request

    def validate(self) -> None:
        for label, value in (("run-id", self.run_id), ("topic", self.topic), ("mode", self.mode)):
            _require_identifier(value, label)
        if _SHA256.fullmatch(self.policy_digest) is None:
            raise RunStateError("invalid-policy-digest", self.policy_digest)


@final
class TopicLock:
    """Held OS byte-range lock; the file itself is permanent and contains no state."""

    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self.path: Path = path
        self.handle: BinaryIO = handle
        self.closed: bool = False

    @classmethod
    def acquire(cls, path: Path, topic: str) -> "TopicLock":
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            _ = handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                _ = handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            _ = handle.seek(0)
            _lock_nonblocking(handle)
        except OSError as error:
            handle.close()
            raise TopicBusyError(topic) from error
        return cls(path, handle)

    def release(self) -> None:
        if self.closed:
            return
        try:
            _ = self.handle.seek(0)
            _unlock(self.handle)
        finally:
            self.handle.close()
            self.closed = True


@final
class RunStateStore:
    """Own run records, active markers, permanent lock files, and corrupt quarantine."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_root: Path | None = None,
        process_probe: ProcessProbe | None = None,
    ) -> None:
        self.root: Path = root.resolve()
        self.runs: Path = self.root / "runs"
        self.topics: Path = self.root / "topics"
        self.locks: Path = self.root / "locks"
        self.quarantine: Path = self.root / "quarantine"
        self.workspace_root: Path = (workspace_root or self.root.parent.parent).resolve()
        self.process_probe: ProcessProbe = process_probe or _probe_process
        for directory in (self.runs, self.topics, self.locks, self.quarantine):
            directory.mkdir(parents=True, exist_ok=True)

    def run_path(self, run_id: str) -> Path:
        _require_identifier(run_id, "run-id")
        return self.runs / f"{run_id}.json"

    def marker_path(self, topic: str) -> Path:
        _require_identifier(topic, "topic")
        return self.topics / f"{topic}.active.json"

    def lock_path(self, topic: str) -> Path:
        _require_identifier(topic, "topic")
        return self.locks / f"{topic}.lock"

    def acquire(self, request: RunRequest) -> "RunLease":
        """Acquire the OS lock before any state read or write, then claim or resume."""
        request.validate()
        topic_lock = TopicLock.acquire(self.lock_path(request.topic), request.topic)
        try:
            marker = self._load_marker_optional(request.topic)
            if marker is None:
                record = self._claim_new(request)
            else:
                record = self._resume_claimed(request, marker)
            return RunLease(self, request, record, _owner_from_record(record), topic_lock)
        except BaseException:
            topic_lock.release()
            raise

    def load(self, run_id: str) -> JsonObject:
        path = self.run_path(run_id)
        if not path.is_file() or path.is_symlink():
            raise RunStateError("state-missing", run_id)
        try:
            value = _load_canonical_object(path)
            _validate_record(value, expected_run_id=run_id)
            return value
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, RunStateError) as error:
            if isinstance(error, RunStateError) and error.code == "state-missing":
                raise
            raise RunStateError("state-corrupt", run_id) from error

    def audit_topic(self, topic: str) -> RunStatus | None:
        """Prove owner death by acquiring the OS lock, then reconcile durably."""
        topic_lock = TopicLock.acquire(self.lock_path(topic), topic)
        try:
            marker = self._load_marker_optional(topic)
            if marker is None:
                return None
            record = self._load_or_recover(marker)
            status = RunStatus(cast(str, record["status"]))
            if status is RunStatus.RUNNING:
                self._require_dead_owner(marker, topic)
                record["status"] = RunStatus.INTERRUPTED.value
                record["heartbeat"] = _utc_now()
                atomic_state_json(self.run_path(cast(str, record["run_id"])), record)
                status = RunStatus.INTERRUPTED
            return status
        finally:
            topic_lock.release()

    def _claim_new(self, request: RunRequest) -> JsonObject:
        path = self.run_path(request.run_id)
        if path.exists() or path.is_symlink():
            try:
                existing = self.load(request.run_id)
            except RunStateError:
                if path.exists() or path.is_symlink():
                    self._quarantine(path, request.run_id)
                existing = _new_record(request, _owner_metadata(), RunStatus.RESUME_REQUIRED)
                atomic_state_json(path, existing)
            return self._resume_record(request, existing)
        orphan = self._unfinished_topic_record(request.topic)
        if orphan is not None:
            orphan_request = RunRequest(
                cast(str, orphan["run_id"]),
                cast(str, orphan["topic"]),
                cast(str, orphan["mode"]),
                cast(str, orphan["policy_digest"]),
            )
            if orphan["status"] == RunStatus.RUNNING.value:
                self._require_dead_owner(orphan, request.topic)
                orphan["status"] = RunStatus.INTERRUPTED.value
                orphan["heartbeat"] = _utc_now()
                atomic_state_json(self.run_path(orphan_request.run_id), orphan)
            atomic_state_json(
                self.marker_path(request.topic),
                _new_marker(orphan_request, _owner_from_record(orphan)),
            )
            raise ResumeRequiredError(request.topic, orphan_request.run_id)
        owner = _owner_metadata()
        record = _new_record(request, owner, RunStatus.RUNNING)
        marker = _new_marker(request, owner)
        atomic_state_json(path, record)
        atomic_state_json(self.marker_path(request.topic), marker)
        return record

    def _resume_claimed(self, request: RunRequest, marker: JsonObject) -> JsonObject:
        marked_run_id = cast(str, marker["run_id"])
        record = self._load_or_recover(marker)
        if record["status"] == RunStatus.SUCCEEDED.value:
            completed = RunRequest(
                marked_run_id,
                cast(str, marker["topic"]),
                cast(str, marker["mode"]),
                cast(str, marker["policy_digest"]),
            )
            self.remove_marker_after_success(completed)
            if request.run_id == marked_run_id:
                raise RunStateError("run-already-succeeded", marked_run_id)
            return self._claim_new(request)
        return self._resume_record(request, record, marked_run_id=marked_run_id)

    def _resume_record(
        self,
        request: RunRequest,
        record: JsonObject,
        *,
        marked_run_id: str | None = None,
    ) -> JsonObject:
        marked_run_id = marked_run_id or cast(str, record["run_id"])
        status = RunStatus(cast(str, record["status"]))
        if status is RunStatus.RUNNING:
            self._require_dead_owner(record, request.topic)
            record["status"] = RunStatus.INTERRUPTED.value
            record["heartbeat"] = _utc_now()
            atomic_state_json(self.run_path(marked_run_id), record)
            status = RunStatus.INTERRUPTED
        if request.run_id != marked_run_id:
            raise ResumeRequiredError(request.topic, marked_run_id)
        if record["topic"] != request.topic or record["mode"] != request.mode:
            raise RunStateError("resume-identity-mismatch", request.run_id)
        if status not in {RunStatus.FAILED, RunStatus.INTERRUPTED, RunStatus.RESUME_REQUIRED, RunStatus.POLICY_DENIED}:
            raise RunStateError("invalid-resume-status", status.value)
        if record["policy_digest"] != request.policy_digest:
            record["policy_digest"] = request.policy_digest
            _invalidate_from(record, 0)
        owner = _owner_metadata()
        _attach_owner(record, owner)
        record["status"] = RunStatus.RUNNING.value
        next_marker = _new_marker(request, owner)
        atomic_state_json(self.run_path(request.run_id), record)
        atomic_state_json(self.marker_path(request.topic), next_marker)
        return record

    def _unfinished_topic_record(self, topic: str) -> JsonObject | None:
        for path in sorted(self.runs.glob("*.json")):
            try:
                record = self.load(path.stem)
            except RunStateError:
                continue
            if record["topic"] == topic and record["status"] not in {
                RunStatus.SUCCEEDED.value,
                RunStatus.DRY_RUN.value,
            }:
                return record
        return None

    def _load_or_recover(self, marker: JsonObject) -> JsonObject:
        request = RunRequest(
            cast(str, marker["run_id"]),
            cast(str, marker["topic"]),
            cast(str, marker["mode"]),
            cast(str, marker["policy_digest"]),
        )
        path = self.run_path(request.run_id)
        try:
            return self.load(request.run_id)
        except RunStateError:
            if path.exists() or path.is_symlink():
                self._quarantine(path, request.run_id)
            recovery = _new_record(request, marker, RunStatus.RESUME_REQUIRED)
            atomic_state_json(path, recovery)
            return recovery

    def _quarantine(self, path: Path, run_id: str) -> None:
        raw = (
            os.readlink(path).encode("utf-8", errors="surrogatepass")
            if path.is_symlink()
            else path.read_bytes()
        )
        digest = hashlib.sha256(raw).hexdigest()
        destination = self.quarantine / f"{run_id}.{digest}.json"
        if destination.exists():
            path.unlink()
        else:
            os.replace(path, destination)

    def _recover_corrupt_marker(self, path: Path, topic: str) -> JsonObject:
        incident_id = uuid.uuid4().hex
        destination = self.quarantine / f"{topic}.marker.{incident_id}"
        os.replace(path, destination)
        recovery_id = f"recovery-{incident_id}"
        request = RunRequest.create(topic, "recovery", "0" * 64, run_id=recovery_id)
        owner = _owner_metadata()
        record = _new_record(request, owner, RunStatus.RESUME_REQUIRED)
        marker = _new_marker(request, owner)
        create_state_json(self.run_path(request.run_id), record)
        atomic_state_json(path, marker)
        return marker

    def _load_marker_optional(self, topic: str) -> JsonObject | None:
        path = self.marker_path(topic)
        if not path.exists() and not path.is_symlink():
            return None
        if not path.is_file() or path.is_symlink():
            return self._recover_corrupt_marker(path, topic)
        try:
            marker = _load_canonical_object(path)
            _validate_marker(marker, topic)
            return marker
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, RunStateError):
            return self._recover_corrupt_marker(path, topic)

    def _require_dead_owner(self, owner: Mapping[str, JsonValue], topic: str) -> None:
        host, pid, start = owner.get("host"), owner.get("pid"), owner.get("process_start")
        if not isinstance(host, str) or not isinstance(pid, int) or not isinstance(start, str):
            raise TopicBusyError(topic)
        identity = self.process_probe(host, pid)
        if identity.alive is False:
            return
        if identity.alive is True and identity.start_token is not None and identity.start_token != start:
            return
        raise TopicBusyError(topic)

    def artifact_valid(self, value: object) -> bool:
        if not _valid_artifact_shape(value):
            return False
        artifact = cast(dict[str, str], value)
        path = _artifact_path(self.workspace_root, artifact["path"])
        return (
            path is not None
            and path.is_file()
            and not path.is_symlink()
            and _file_sha256(path) == artifact["sha256"]
        )

    def remove_marker_after_success(self, request: RunRequest) -> None:
        marker = self._load_marker_optional(request.topic)
        if marker is not None and marker.get("run_id") == request.run_id:
            self.marker_path(request.topic).unlink(missing_ok=True)


@final
class RunLease:
    """Only mutator for a run while its topic OS lock is held."""

    def __init__(
        self,
        store: RunStateStore,
        request: RunRequest,
        record: JsonObject,
        owner: JsonObject,
        topic_lock: TopicLock,
    ) -> None:
        self.store: RunStateStore = store
        self.request: RunRequest = request
        self.record: JsonObject = record
        self.owner: JsonObject = owner
        self.topic_lock: TopicLock = topic_lock
        self.finished: bool = False
        self.closed: bool = False

    def __enter__(self) -> "RunLease":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.finished:
            self.finish(RunStatus.FAILED if exc_type is not None else RunStatus.INTERRUPTED)
        self.release()

    def heartbeat(self) -> None:
        self._require_running()
        now = _utc_now()
        self.record["heartbeat"] = now
        marker = _new_marker(self.request, self.owner)
        marker["heartbeat"] = now
        atomic_state_json(self.store.run_path(self.request.run_id), self.record)
        atomic_state_json(self.store.marker_path(self.request.topic), marker)

    def set_stage_inputs(self, stage: str, inputs: Mapping[str, str]) -> None:
        self._require_running()
        _require_identifier(stage, "stage")
        normalized = _validated_inputs(inputs)
        stages = cast(dict[str, JsonValue], self.record["stages"])
        current = stages.get(stage)
        if isinstance(current, dict):
            order = cast(int, current["order"])
            if current["inputs"] != normalized:
                _invalidate_from(self.record, order)
                stages = cast(dict[str, JsonValue], self.record["stages"])
                stages[stage] = _stage_record(order, normalized)
        else:
            order = 1 + max((cast(int, cast(dict[str, JsonValue], value)["order"]) for value in stages.values()), default=-1)
            stages[stage] = _stage_record(order, normalized)
        atomic_state_json(self.store.run_path(self.request.run_id), self.record)

    def stage_reusable(self, stage: str, inputs: Mapping[str, str]) -> bool:
        self._require_running()
        _require_identifier(stage, "stage")
        normalized = _validated_inputs(inputs)
        stages = cast(dict[str, JsonValue], self.record["stages"])
        raw = stages.get(stage)
        if not isinstance(raw, dict) or raw.get("inputs") != normalized or raw.get("status") != "succeeded":
            if isinstance(raw, dict) and raw.get("inputs") != normalized:
                self.set_stage_inputs(stage, inputs)
            return False
        artifacts = raw.get("artifacts")
        if not isinstance(artifacts, list) or not all(self.store.artifact_valid(item) for item in artifacts):
            _invalidate_from(self.record, cast(int, raw["order"]))
            atomic_state_json(self.store.run_path(self.request.run_id), self.record)
            return False
        return True

    def complete_stage(self, stage: str, artifacts: list[Mapping[str, str]]) -> None:
        self._require_running()
        _require_identifier(stage, "stage")
        stages = cast(dict[str, JsonValue], self.record["stages"])
        raw = stages.get(stage)
        if not isinstance(raw, dict):
            raise RunStateError("stage-inputs-missing", stage)
        normalized: list[JsonValue] = []
        for artifact in artifacts:
            path = artifact.get("path")
            digest = artifact.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                raise RunStateError("artifact-invalid", stage)
            item: JsonObject = {"path": path, "sha256": digest}
            if not self.store.artifact_valid(item):
                raise RunStateError("artifact-invalid", path)
            normalized.append(item)
        raw["artifacts"] = normalized
        raw["status"] = "succeeded"
        atomic_state_json(self.store.run_path(self.request.run_id), self.record)

    def finish(self, status: RunStatus) -> None:
        self._require_running()
        if status not in {RunStatus.DRY_RUN, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.POLICY_DENIED, RunStatus.INTERRUPTED}:
            raise RunStateError("invalid-terminal-status", status.value)
        if status is RunStatus.SUCCEEDED:
            stages = cast(dict[str, JsonValue], self.record["stages"])
            if any(cast(dict[str, JsonValue], stage)["status"] != "succeeded" for stage in stages.values()):
                raise RunStateError("incomplete-stages", self.request.run_id)
            if any(
                not all(self.store.artifact_valid(item) for item in cast(list[JsonValue], cast(dict[str, JsonValue], stage)["artifacts"]))
                for stage in stages.values()
            ):
                raise RunStateError("artifact-drift", self.request.run_id)
        self.record["status"] = status.value
        self.record["heartbeat"] = _utc_now()
        atomic_state_json(self.store.run_path(self.request.run_id), self.record)
        if status is RunStatus.SUCCEEDED:
            self.store.remove_marker_after_success(self.request)
        self.finished = True

    def release(self) -> None:
        if self.closed:
            return
        if not self.finished and self.record["status"] == RunStatus.RUNNING.value:
            self.record["status"] = RunStatus.INTERRUPTED.value
            self.record["heartbeat"] = _utc_now()
            atomic_state_json(self.store.run_path(self.request.run_id), self.record)
            self.finished = True
        self.topic_lock.release()
        self.closed = True

    def _require_running(self) -> None:
        if self.closed:
            raise RunStateError("lease-closed", self.request.run_id)
        if self.finished or self.record["status"] != RunStatus.RUNNING.value:
            raise RunStateError("run-not-running", self.request.run_id)



def _new_marker(request: RunRequest, owner: Mapping[str, JsonValue]) -> JsonObject:
    return {
        "heartbeat": owner["heartbeat"],
        "host": owner["host"],
        "mode": request.mode,
        "pid": owner["pid"],
        "policy_digest": request.policy_digest,
        "process_start": owner["process_start"],
        "run_id": request.run_id,
        "schema": MARKER_SCHEMA,
        "schema_version": STATE_VERSION,
        "started_at": owner["started_at"],
        "topic": request.topic,
    }


def _new_record(request: RunRequest, owner: Mapping[str, JsonValue], status: RunStatus) -> JsonObject:
    return {
        "heartbeat": owner["heartbeat"],
        "host": owner["host"],
        "mode": request.mode,
        "pid": owner["pid"],
        "policy_digest": request.policy_digest,
        "process_start": owner["process_start"],
        "run_id": request.run_id,
        "schema": STATE_SCHEMA,
        "schema_version": STATE_VERSION,
        "stages": {},
        "started_at": owner["started_at"],
        "status": status.value,
        "topic": request.topic,
    }


def _owner_metadata() -> JsonObject:
    now = _utc_now()
    return {
        "heartbeat": now,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "process_start": _process_start_token(os.getpid()) or f"unknown-{uuid.uuid4().hex}",
        "started_at": now,
    }


def _owner_from_record(record: Mapping[str, JsonValue]) -> JsonObject:
    return {
        key: record[key]
        for key in ("heartbeat", "host", "pid", "process_start", "started_at")
    }


def _attach_owner(record: JsonObject, owner: Mapping[str, JsonValue]) -> None:
    for key in ("heartbeat", "host", "pid", "process_start", "started_at"):
        record[key] = owner[key]


def _stage_record(order: int, inputs: JsonObject) -> JsonObject:
    return {"artifacts": [], "inputs": inputs, "order": order, "status": "pending"}


def _invalidate_from(record: JsonObject, order: int) -> None:
    stages = cast(dict[str, JsonValue], record["stages"])
    for raw in stages.values():
        stage = cast(dict[str, JsonValue], raw)
        if cast(int, stage["order"]) >= order:
            stage["artifacts"] = []
            stage["status"] = "pending"


def _validated_inputs(inputs: Mapping[str, str]) -> JsonObject:
    result: JsonObject = {}
    for label, digest in inputs.items():
        if _HASH_LABEL.fullmatch(label) is None or _SHA256.fullmatch(digest) is None:
            raise RunStateError("stage-input-invalid", label)
        result[label] = digest
    return result


def _validate_record(value: JsonObject, *, expected_run_id: str) -> None:
    required = {
        "heartbeat", "host", "mode", "pid", "policy_digest", "process_start", "run_id", "schema",
        "schema_version", "stages", "started_at", "status", "topic",
    }
    if set(value) != required or value.get("schema") != STATE_SCHEMA or value.get("schema_version") != STATE_VERSION:
        raise RunStateError("record-shape", expected_run_id)
    if value.get("run_id") != expected_run_id:
        raise RunStateError("record-run-id", expected_run_id)
    _validate_common(value)
    status = value.get("status")
    if not isinstance(status, str) or status not in {item.value for item in RunStatus}:
        raise RunStateError("record-status", expected_run_id)
    stages = value.get("stages")
    if not isinstance(stages, dict):
        raise RunStateError("record-stages", expected_run_id)
    orders: set[int] = set()
    for name, raw in stages.items():
        if _IDENTIFIER.fullmatch(name) is None or not isinstance(raw, dict):
            raise RunStateError("stage-shape", str(name))
        if set(raw) != {"artifacts", "inputs", "order", "status"}:
            raise RunStateError("stage-shape", name)
        order, stage_status, inputs, artifacts = raw["order"], raw["status"], raw["inputs"], raw["artifacts"]
        if not isinstance(order, int) or isinstance(order, bool) or order < 0 or order in orders:
            raise RunStateError("stage-order", name)
        orders.add(order)
        if not isinstance(stage_status, str) or stage_status not in _STAGE_STATUSES:
            raise RunStateError("stage-status", name)
        if not isinstance(inputs, dict) or not _valid_inputs_object(inputs):
            raise RunStateError("stage-inputs", name)
        if not isinstance(artifacts, list) or not all(_valid_artifact_shape(item) for item in artifacts):
            raise RunStateError("stage-artifacts", name)


def _validate_marker(value: JsonObject, topic: str) -> None:
    required = {
        "heartbeat", "host", "mode", "pid", "policy_digest", "process_start", "run_id", "schema",
        "schema_version", "started_at", "topic",
    }
    if set(value) != required or value.get("schema") != MARKER_SCHEMA or value.get("schema_version") != STATE_VERSION:
        raise RunStateError("marker-shape", topic)
    if value.get("topic") != topic:
        raise RunStateError("marker-topic", topic)
    _validate_common(value)


def _validate_common(value: Mapping[str, JsonValue]) -> None:
    for key in ("run_id", "topic", "mode"):
        raw = value.get(key)
        if not isinstance(raw, str) or _IDENTIFIER.fullmatch(raw) is None:
            raise RunStateError("identity-invalid", key)
    policy_digest = value.get("policy_digest")
    if not isinstance(policy_digest, str) or _SHA256.fullmatch(policy_digest) is None:
        raise RunStateError("policy-invalid", "policy-digest")
    if not isinstance(value.get("host"), str) or not isinstance(value.get("pid"), int) or isinstance(value.get("pid"), bool):
        raise RunStateError("owner-invalid", "host/pid")
    process_start = value.get("process_start")
    if not isinstance(process_start, str) or not process_start:
        raise RunStateError("owner-invalid", "process-start")
    for key in ("started_at", "heartbeat"):
        raw = value.get(key)
        if not isinstance(raw, str):
            raise RunStateError("owner-invalid", key)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise RunStateError("owner-invalid", key) from error
        if parsed.tzinfo is None:
            raise RunStateError("owner-invalid", key)


def _valid_inputs_object(value: Mapping[str, JsonValue]) -> bool:
    return all(
        isinstance(digest, str)
        and _HASH_LABEL.fullmatch(label) is not None
        and _SHA256.fullmatch(digest) is not None
        for label, digest in value.items()
    )


def _valid_artifact_shape(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    artifact = cast(dict[object, object], value)
    if set(artifact) != {"path", "sha256"}:
        return False
    path, digest = artifact.get("path"), artifact.get("sha256")
    return isinstance(path, str) and isinstance(digest, str) and _safe_relative_path(path) and _SHA256.fullmatch(digest) is not None


def _safe_relative_path(value: str) -> bool:
    if "\\" in value or not value or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return False
    return all(part not in {"", ".", ".."} for part in PurePosixPath(value).parts)


def _artifact_path(workspace_root: Path, value: str) -> Path | None:
    if not _safe_relative_path(value):
        return None
    candidate = (workspace_root / PurePosixPath(value)).resolve()
    try:
        _ = candidate.relative_to(workspace_root)
    except ValueError:
        return None
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_identifier(value: str, label: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise RunStateError(f"invalid-{label}", value)


def _load_canonical_object(path: Path) -> JsonObject:
    raw = path.read_bytes()
    value = cast(JsonValue, json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant))
    if not isinstance(value, dict) or raw != _canonical(value):
        raise RunStateError("json-noncanonical", str(path))
    return value


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> JsonValue:
    raise ValueError(f"invalid JSON constant: {value}")


def _canonical(value: JsonValue) -> bytes:
    return (json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_state_json(path: Path, value: JsonObject) -> None:
    payload = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    temporary = temporary_root / "record.json"
    try:
        with temporary.open("xb") as stream:
            _ = stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(temporary, path)
        if _load_canonical_object(path) != value:
            raise RunStateError("state-publish-invalid", str(path))
    finally:
        try:
            temporary.unlink(missing_ok=True)
            temporary_root.rmdir()
        except OSError:
            pass


def create_state_json(path: Path, value: JsonObject) -> None:
    """Publish a complete canonical record only if its final name is absent."""
    payload = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    temporary = temporary_root / "record.json"
    try:
        with temporary.open("xb") as stream:
            _ = stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        with path.open("r+b") as published:
            published.flush()
            os.fsync(published.fileno())
        if _load_canonical_object(path) != value:
            raise RunStateError("state-publish-invalid", str(path))
    finally:
        try:
            temporary.unlink(missing_ok=True)
            temporary_root.rmdir()
        except OSError:
            pass


def _lock_nonblocking(handle: BinaryIO) -> None:
    if os.name == "nt":
        module = cast(_Msvcrt, cast(object, importlib.import_module("msvcrt")))
        module.locking(handle.fileno(), module.LK_NBLCK, 1)
    else:
        module = cast(_Fcntl, cast(object, importlib.import_module("fcntl")))
        module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)


def durable_replace(source: Path, destination: Path) -> None:
    """Publish one file with write-through rename and directory durability."""
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file.restype = wintypes.BOOL
        moved = cast(
            bool,
            cast(
                object,
                move_file(
                    str(source),
                    str(destination),
                    0x1 | 0x8,
                ),
            ),
        )
        if not moved:
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    with destination.open("r+b") as published:
        published.flush()
        os.fsync(published.fileno())


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        module = cast(_Msvcrt, cast(object, importlib.import_module("msvcrt")))
        module.locking(handle.fileno(), module.LK_UNLCK, 1)
    else:
        module = cast(_Fcntl, cast(object, importlib.import_module("fcntl")))
        module.flock(handle.fileno(), module.LOCK_UN)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _probe_process(host: str, pid: int) -> ProcessIdentity:
    if host != socket.gethostname():
        return ProcessIdentity(None, None)
    if pid <= 0:
        return ProcessIdentity(False, None)
    if os.name == "nt":
        return _windows_process_identity(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return ProcessIdentity(None, None)
    except OSError:
        return ProcessIdentity(False, None)
    token = _process_start_token(pid)
    return ProcessIdentity(True if token is not None else None, token)


def _process_start_token(pid: int) -> str | None:
    if os.name == "nt":
        return _windows_process_identity(pid).start_token
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]
    except (OSError, IndexError):
        return None


def _windows_process_identity(pid: int) -> ProcessIdentity:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = cast(int, cast(object, open_process(0x1000, False, pid)))
    if not handle:
        error = ctypes.get_last_error()
        return ProcessIdentity(False if error == 87 else None, None)
    exit_code = wintypes.DWORD()
    created, exited, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    try:
        exit_ok = cast(bool, cast(object, get_exit_code(handle, ctypes.byref(exit_code))))
        if not exit_ok:
            return ProcessIdentity(None, None)
        if exit_code.value != 259:
            return ProcessIdentity(False, None)
        times_ok = cast(
            bool,
            cast(
                object,
                get_process_times(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ),
            ),
        )
        if not times_ok:
            return ProcessIdentity(None, None)
        token = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        return ProcessIdentity(True, token)
    finally:
        _ = cast(object, close_handle(handle))
