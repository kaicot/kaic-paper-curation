"""Atomic, provider-safe cache for completed Codex generation envelopes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Literal, TypeAlias, cast, final, override

from pipeline.providers.codex_gateway import CodexGateway, CodexRole
from pipeline.runtime_policy import RuntimePolicy
from pipeline.schemas.codex_schema import JsonObject, JsonValue


CACHE_SCHEMA: Final = "generation-cache-v1"
CACHE_SCHEMA_VERSION: Final = 1
_DIGEST_SIZE: Final = 64
_MAX_ENVELOPE_BYTES: Final = 1_048_576
FailureStatus: TypeAlias = Literal["denied", "cancelled", "failed", "partial"]


@dataclass(frozen=True, slots=True)
class GenerationCacheError(RuntimeError):
    """A cache boundary rejected an invalid identity or non-success outcome."""

    code: str
    detail: str

    @override
    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class CacheIdentity:
    """Every input whose change must make a generation result ineligible."""

    runtime_mode: str
    capability: str
    role: str
    model: str
    reasoning_effort: str
    cli_version: str
    signed_binary_sha256: str
    attestation_sha256: str
    contract_sha256: str
    policy_version: str
    policy_sha256: str
    prompt_version: str
    prompt_sha256: str
    schema_version: str
    schema_sha256: str
    source_sha256: str
    task_id: str

    @classmethod
    def from_gateway(
        cls,
        *,
        runtime_policy: RuntimePolicy,
        gateway: CodexGateway,
        role: CodexRole,
        prompt_version: str,
        prompt: str,
        schema_version: str,
        schema: JsonObject,
        source: bytes,
        task_id: str,
    ) -> "CacheIdentity":
        """Derive trust fields from one freshly verified Codex attestation."""
        if runtime_policy.mode != "codex" or runtime_policy.allow_paid_api is not False:
            raise GenerationCacheError("policy-denied", "Codex generation is not allowed")
        before_bytes = _read_regular_bytes(gateway.paths.attestation)
        attestation = _strict_object(before_bytes)
        inventory = gateway.capability_inventory()
        after_bytes = _read_regular_bytes(gateway.paths.attestation)
        if before_bytes != after_bytes:
            raise GenerationCacheError("attestation-race", "Codex attestation changed during verification")

        contract_sha256 = _sha256(_canonical(gateway.contract))
        policy_sha256 = _sha256(_canonical(gateway.policy))
        roles = inventory.get("roles")
        role_contract = roles.get(role) if isinstance(roles, dict) else None
        if (
            inventory.get("attested") is not True
            or inventory.get("paid_api") is not False
            or inventory.get("provider") != "saved-chatgpt-auth-codex-cli"
            or inventory.get("cli_version") != attestation.get("cli_version")
            or inventory.get("contract_sha256") != contract_sha256
            or inventory.get("policy_sha256") != policy_sha256
            or attestation.get("contract_sha256") != contract_sha256
            or attestation.get("policy_sha256") != policy_sha256
            or not isinstance(role_contract, dict)
        ):
            raise GenerationCacheError("attestation-invalid", "Codex cache trust inputs are inconsistent")
        model, effort = role_contract.get("model"), role_contract.get("reasoning_effort")
        binary_sha256, cli_version = attestation.get("binary_sha256"), attestation.get("cli_version")
        if (
            not isinstance(model, str)
            or not isinstance(effort, str)
            or not isinstance(binary_sha256, str)
            or not isinstance(cli_version, str)
        ):
            raise GenerationCacheError("attestation-invalid", "Codex cache trust inputs are incomplete")
        runtime_version, codex_policy_version = runtime_policy.schema_version, gateway.policy.get("schema_version")
        if not isinstance(codex_policy_version, int):
            raise GenerationCacheError("attestation-invalid", "Codex policy version is invalid")
        return cls(
            runtime_mode=runtime_policy.mode,
            capability="codex_generation",
            role=role,
            model=model,
            reasoning_effort=effort,
            cli_version=cli_version,
            signed_binary_sha256=binary_sha256,
            attestation_sha256=_sha256(_canonical(attestation)),
            contract_sha256=contract_sha256,
            policy_version=f"runtime-v{runtime_version}/codex-v{codex_policy_version}",
            policy_sha256=policy_sha256,
            prompt_version=prompt_version,
            prompt_sha256=_sha256(prompt.encode("utf-8")),
            schema_version=schema_version,
            schema_sha256=_sha256(_canonical(schema)),
            source_sha256=_sha256(source),
            task_id=task_id,
        )

    def __post_init__(self) -> None:
        text_values = (
            self.runtime_mode, self.capability, self.role, self.model,
            self.reasoning_effort, self.cli_version, self.policy_version,
            self.prompt_version, self.schema_version, self.task_id,
        )
        if not all(value.strip() for value in text_values):
            raise GenerationCacheError("identity-invalid", "identity text fields must be non-empty")
        digest_values = (
            self.signed_binary_sha256, self.attestation_sha256,
            self.contract_sha256, self.policy_sha256, self.prompt_sha256,
            self.schema_sha256, self.source_sha256,
        )
        if not all(_is_sha256(value) for value in digest_values):
            raise GenerationCacheError("identity-invalid", "identity digests must be SHA-256 hex")

    def as_json(self) -> JsonObject:
        """Return the canonical, non-secret identity envelope content."""
        return {
            "attestation_sha256": self.attestation_sha256,
            "capability": self.capability,
            "cli_version": self.cli_version,
            "contract_sha256": self.contract_sha256,
            "model": self.model,
            "policy_sha256": self.policy_sha256,
            "policy_version": self.policy_version,
            "prompt_sha256": self.prompt_sha256,
            "prompt_version": self.prompt_version,
            "reasoning_effort": self.reasoning_effort,
            "role": self.role,
            "runtime_mode": self.runtime_mode,
            "schema_sha256": self.schema_sha256,
            "schema_version": self.schema_version,
            "signed_binary_sha256": self.signed_binary_sha256,
            "source_sha256": self.source_sha256,
            "task_id": self.task_id,
        }

    @property
    def digest(self) -> str:
        """Derive the content-addressed filename from every safety dimension."""
        return _sha256(_canonical(self.as_json()))


@dataclass(frozen=True, slots=True)
class CacheSuccess:
    """A locally validated result that may be published as a cache hit."""

    result: JsonObject


@dataclass(frozen=True, slots=True)
class CacheFailure:
    """A terminal non-success outcome that must never be published."""

    status: FailureStatus


GenerationOutcome: TypeAlias = CacheSuccess | CacheFailure
BeforePublish: TypeAlias = Callable[[Path], None]
Generator: TypeAlias = Callable[[], GenerationOutcome]


@final
class GenerationCache:
    """Read complete envelopes and atomically publish only successful results."""

    def __init__(self, directory: Path, before_publish: BeforePublish | None = None) -> None:
        self.directory: Path = directory
        self.before_publish: BeforePublish | None = before_publish

    def load(self, identity: CacheIdentity) -> JsonObject | None:
        """Return an exact valid success envelope, treating every other file as a miss."""
        path = self._path(identity)
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_ENVELOPE_BYTES:
                return None
            payload = _load_json(path)
            return _read_envelope(payload, identity)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, GenerationCacheError):
            return None

    def get_or_generate(self, identity: CacheIdentity, generate: Generator) -> JsonObject:
        """Reuse an exact completed result or publish one validated producer success."""
        hit = self.load(identity)
        if hit is not None:
            return hit
        outcome = generate()
        if isinstance(outcome, CacheSuccess):
            self._publish(identity, outcome.result)
            return outcome.result
        raise GenerationCacheError(f"generation-{outcome.status}", "generation did not succeed")

    def _path(self, identity: CacheIdentity) -> Path:
        return self.directory / f"{identity.digest}.json"

    def _publish(self, identity: CacheIdentity, result: JsonObject) -> None:
        if not _is_json_value(result):
            raise GenerationCacheError("result-invalid", "successful result must be a strict JSON object")
        payload = _success_envelope(identity, result)
        encoded = _canonical(payload)
        if len(encoded) > _MAX_ENVELOPE_BYTES:
            raise GenerationCacheError("result-limit", "success envelope exceeds cache limit")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix=f".{identity.digest}.", dir=self.directory))
        temporary = temporary_root / "envelope.json"
        try:
            with temporary.open("xb") as handle:
                _ = handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _require_envelope(temporary, identity)
            if self.before_publish is not None:
                self.before_publish(temporary)
            _require_envelope(temporary, identity)
            os.replace(temporary, self._path(identity))
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)


def _success_envelope(identity: CacheIdentity, result: JsonObject) -> JsonObject:
    return {
        "identity": identity.as_json(),
        "identity_sha256": identity.digest,
        "result": result,
        "result_sha256": _sha256(_canonical(result)),
        "schema": CACHE_SCHEMA,
        "schema_version": CACHE_SCHEMA_VERSION,
        "state": "succeeded",
    }


def _read_envelope(payload: JsonValue, identity: CacheIdentity) -> JsonObject | None:
    if not isinstance(payload, dict):
        return None
    expected = {"identity", "identity_sha256", "result", "result_sha256", "schema", "schema_version", "state"}
    if set(payload) != expected:
        return None
    result = payload["result"]
    if not isinstance(result, dict) or not _is_json_value(result):
        return None
    if (
        payload["schema"] != CACHE_SCHEMA
        or payload["schema_version"] != CACHE_SCHEMA_VERSION
        or payload["state"] != "succeeded"
        or payload["identity"] != identity.as_json()
        or payload["identity_sha256"] != identity.digest
        or payload["result_sha256"] != _sha256(_canonical(result))
    ):
        return None
    return result


def _load_json(path: Path) -> JsonValue:
    raw = path.read_bytes()
    value = _strict_json(raw)
    if raw != _canonical(value):
        raise GenerationCacheError("json-noncanonical", "cache envelope is not canonical JSON")
    return value


def _canonical(value: JsonValue) -> bytes:
    try:
        text = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise GenerationCacheError("json-invalid", "value is not strict JSON") from error
    return (text + "\n").encode("utf-8")


def _read_regular_bytes(path: Path) -> bytes:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_ENVELOPE_BYTES:
            raise GenerationCacheError("invalid-file", "required regular file is unavailable")
        return path.read_bytes()
    except OSError as error:
        raise GenerationCacheError("invalid-file", "required regular file is unavailable") from error


def _strict_object(raw: bytes) -> JsonObject:
    value = _strict_json(raw)
    if raw != _canonical(value):
        raise GenerationCacheError("json-noncanonical", "required JSON object is not canonical")
    if not isinstance(value, dict):
        raise GenerationCacheError("invalid-json", "required JSON object is unavailable")
    return value


def _strict_json(raw: bytes) -> JsonValue:
    try:
        value = cast(
            JsonValue,
            json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"invalid JSON constant: {item}")),
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise GenerationCacheError("invalid-json", "strict JSON is unavailable") from error
    if not _is_json_value(value):
        raise GenerationCacheError("invalid-json", "strict JSON is unavailable")
    return value


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _require_envelope(path: Path, identity: CacheIdentity) -> None:
    try:
        payload = _load_json(path)
        if _read_envelope(payload, identity) is None:
            raise GenerationCacheError("envelope-invalid", "cache envelope changed before publication")
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, GenerationCacheError) as error:
        raise GenerationCacheError("envelope-invalid", "cache envelope changed before publication") from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == _DIGEST_SIZE and all(char in "0123456789abcdef" for char in value)


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        values = cast(dict[object, object], value)
        return all(isinstance(key, str) and _is_json_value(item) for key, item in values.items())
    return False
