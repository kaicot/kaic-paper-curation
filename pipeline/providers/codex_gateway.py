"""Fail-closed saved-ChatGPT-auth Codex CLI generation boundary."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Protocol

from pipeline.model_config import (
    CodexRole,
    load_model_config,
    load_role_config,
    load_role_models,
    role_fingerprint,
)
from pipeline.schemas.codex_schema import JsonObject, JsonValue, SchemaError, validate_json


EXPECTED_AUTH: Final = "Logged in using ChatGPT"
TRUSTED_OPENAI_SIGNERS: Final = frozenset({
    'CN="OpenAI OpCo, LLC", O="OpenAI OpCo, LLC", L=San Francisco, S=California, C=US',
})
ENVIRONMENT_KEYS: Final = (
    "SystemRoot", "WINDIR", "ComSpec", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PATH", "LANG", "LC_ALL",
)
REQUIRED_EXEC_OPTIONS: Final = (
    "--ignore-user-config", "--ignore-rules", "--sandbox", "--ephemeral",
    "--output-schema", "--output-last-message",
)
PRIMARY_INSTALL_RELATIVE: Final = "Programs/OpenAI/Codex/bin/codex.exe"
RELEASE_ROOT_RELATIVE: Final = ".codex/packages/standalone/releases"
RELEASE_EXECUTABLE_RELATIVE: Final = "bin/codex.exe"
# Compatibility export only. Gateway instances take their own repository-root snapshot.
ROLE_MODELS: dict[str, tuple[str, str]] = load_role_models()


@dataclass(slots=True)
class CodexGatewayError(RuntimeError):
    """A sanitized gateway failure safe to return across application boundaries."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    argv: tuple[str, ...]
    stdin: bytes
    cwd: Path
    environment: dict[str, str]
    timeout_seconds: int


class ProcessRunner(Protocol):
    def run(self, request: ProcessRequest) -> subprocess.CompletedProcess[bytes]: ...


class SubprocessRunner:
    """Wait for the direct Codex process without inheritable pipe-drain hangs."""

    _OUTPUT_LIMIT: Final = 1_048_576

    @classmethod
    def _read_tail(cls, handle: BinaryIO) -> bytes:
        handle.flush()
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - cls._OUTPUT_LIMIT))
        return handle.read(cls._OUTPUT_LIMIT)

    def run(self, request: ProcessRequest) -> subprocess.CompletedProcess[bytes]:
        # File handles let wait() track only the direct child. If a hook or helper
        # inherits stdout/stderr, it cannot keep Python blocked in communicate().
        with tempfile.TemporaryFile() as stdin_handle, tempfile.TemporaryFile() as stdout_handle, tempfile.TemporaryFile() as stderr_handle:
            stdin_handle.write(request.stdin)
            stdin_handle.seek(0)
            process = subprocess.Popen(
                request.argv,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=request.cwd,
                env=request.environment,
                close_fds=True,
            )
            try:
                returncode = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                raise subprocess.TimeoutExpired(request.argv, request.timeout_seconds)
            return subprocess.CompletedProcess(
                request.argv,
                returncode,
                self._read_tail(stdout_handle),
                self._read_tail(stderr_handle),
            )


@dataclass(frozen=True, slots=True)
class GatewayPaths:
    repository: Path
    executable: Path
    attestation: Path
    testing: bool = False
    testing_release: Path | None = None


@dataclass(frozen=True, slots=True)
class _Runtime:
    executable: Path
    identity: JsonObject


def _digest(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise CodexGatewayError("invalid-file", "required regular file is unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: JsonValue) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_object(path: Path) -> JsonObject:
    try:
        value: JsonValue = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexGatewayError("invalid-json", "required JSON object is unavailable") from error
    if not isinstance(value, dict):
        raise CodexGatewayError("invalid-json", "required JSON object is unavailable")
    return value


def _known_folder(csidl: int) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise CodexGatewayError("known-folder", "Windows known-folder lookup failed")
    return buffer.value


def _windows_directory() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise CodexGatewayError("windows-directory", "Windows directory lookup failed")
    return Path(buffer.value)


def _closed_environment(executable: Path, temporary: Path) -> dict[str, str]:
    profile, local_appdata, appdata = _known_folder(40), _known_folder(28), _known_folder(26)
    drive, tail = os.path.splitdrive(profile)
    windows = _windows_directory()
    environment = {
        "SystemRoot": str(windows), "WINDIR": str(windows), "ComSpec": str(windows / "System32/cmd.exe"), "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": str(temporary), "TMP": str(temporary), "USERPROFILE": profile,
        "LOCALAPPDATA": local_appdata, "APPDATA": appdata, "HOMEDRIVE": drive,
        "HOMEPATH": tail, "OS": "Windows_NT", "PROCESSOR_ARCHITECTURE": platform.machine(),
        "NUMBER_OF_PROCESSORS": str(os.cpu_count() or 1),
        "PATH": f"{executable.parent};{windows / 'System32'}",
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
    }
    if tuple(environment) != ENVIRONMENT_KEYS:
        raise CodexGatewayError("environment-contract", "closed environment key drift")
    return environment


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _has_reparse_point(path: Path) -> bool:
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class CodexGateway:
    """Discover, qualify, and pin one signed Codex binary for each generation."""

    def __init__(self, paths: GatewayPaths, runner: ProcessRunner | None = None) -> None:
        self.paths = paths
        self.runner = runner or SubprocessRunner()
        self.contract_path = paths.repository / "pipeline/codex-cli-contract.json"
        self.policy_path = paths.repository / "pipeline/codex-cli-policy.json"
        self.contract, self.policy = _load_object(self.contract_path), _load_object(self.policy_path)
        try:
            self.model_config = load_model_config(paths.repository)
            self.role_models = load_role_models(paths.repository)
            self.role_configs = {role: load_role_config(role, paths.repository) for role in self.role_models}
            self.role_fingerprints = {role: role_fingerprint(role, paths.repository) for role in self.role_models}
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise CodexGatewayError("model-config", "model routing configuration is invalid") from error
        self._identity_cache: dict[tuple[str, int, int], JsonObject] = {}
        self.last_run_metrics: JsonObject | None = None
        self.last_run_provenance: JsonObject | None = None
        self._validate_checked_contract()

    @classmethod
    def production(cls, repository: Path | None = None) -> "CodexGateway":
        root = (repository or Path(__file__).resolve().parents[2]).resolve()
        policy = _load_object(root / "pipeline/codex-cli-policy.json")
        attestation = policy.get("attestation_path")
        if not isinstance(attestation, str):
            raise CodexGatewayError("policy-drift", "Codex attestation path is invalid")
        executable = cls._discover_production_candidate(policy)
        return cls(GatewayPaths(root, executable, root / attestation))

    @classmethod
    def for_testing(cls, paths: GatewayPaths, runner: ProcessRunner) -> "CodexGateway":
        resolved_root = paths.attestation.parent.resolve(strict=True)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        if os.environ.get("PAPER_CURATION_TESTING") != "1" or temp_root not in resolved_root.parents:
            raise CodexGatewayError("test-override", "testing override requires a unique temporary root")
        resolved_executable = paths.executable.resolve(strict=True)
        if resolved_root not in resolved_executable.parents:
            raise CodexGatewayError("test-override", "testing executable must be inside its unique root")
        testing_release = paths.testing_release.resolve(strict=True) if paths.testing_release is not None else None
        if testing_release is not None and resolved_root not in testing_release.parents:
            raise CodexGatewayError("test-override", "testing release must be inside its unique root")
        return cls(GatewayPaths(paths.repository.resolve(), resolved_executable, paths.attestation.resolve(), True, testing_release), runner)

    @staticmethod
    def _discover_production_candidate(policy: JsonObject) -> Path:
        discovery = policy.get("discovery")
        if not isinstance(discovery, dict):
            raise CodexGatewayError("policy-drift", "Codex discovery policy is invalid")
        primary_relative = discovery.get("primary_local_appdata_relative")
        release_relative = discovery.get("release_root_userprofile_relative")
        executable_relative = discovery.get("release_executable_relative")
        if not all(isinstance(value, str) and value for value in (primary_relative, release_relative, executable_relative)):
            raise CodexGatewayError("policy-drift", "Codex discovery policy is invalid")
        primary = Path(_known_folder(28)) / str(primary_relative)
        if primary.is_file() and not primary.is_symlink() and not _has_reparse_point(primary):
            return primary.resolve(strict=True)
        release_root = Path(_known_folder(40)) / str(release_relative)
        candidates = sorted(
            (path / str(executable_relative) for path in release_root.glob("*") if path.is_dir()),
            key=lambda path: path.parent.parent.name,
            reverse=True,
        )
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink() and not _has_reparse_point(candidate):
                return candidate.resolve(strict=True)
        raise CodexGatewayError("codex-not-found", "no official Codex installation candidate is available")

    def _validate_checked_contract(self) -> None:
        forbidden_policy = {"canonical_executable", "canonical_final_executable", "cli_version", "binary_sha256"}
        if forbidden_policy.intersection(self.policy):
            raise CodexGatewayError("policy-drift", "version-specific Codex policy is forbidden")
        if self.policy.get("schema") != "codex-cli-policy-v2" or self.policy.get("schema_version") != 2:
            raise CodexGatewayError("policy-drift", "unsupported Codex policy schema")
        signers = self.policy.get("trusted_signers")
        if not isinstance(signers, list) or frozenset(signers) != TRUSTED_OPENAI_SIGNERS:
            raise CodexGatewayError("policy-drift", "OpenAI signer policy drift")
        if self.policy.get("allow_paid_api") is not False or self.policy.get("fast_mode") is not False or self.policy.get("auto_qualify_on_generation") is not True:
            raise CodexGatewayError("policy-drift", "Codex provider safety policy drift")
        discovery = self.policy.get("discovery")
        if discovery != {
            "primary_local_appdata_relative": PRIMARY_INSTALL_RELATIVE,
            "release_executable_relative": RELEASE_EXECUTABLE_RELATIVE,
            "release_root_userprofile_relative": RELEASE_ROOT_RELATIVE,
        }:
            raise CodexGatewayError("policy-drift", "official Codex installation discovery drift")
        if self.contract.get("schema") != "codex-cli-contract-v2" or self.contract.get("schema_version") != 2:
            raise CodexGatewayError("contract-drift", "unsupported Codex contract schema")
        if self.contract.get("cache_compatibility_epoch") != self.model_config.get("cache_epoch"):
            raise CodexGatewayError("contract-drift", "generation compatibility epoch drift")
        if "roles" in self.contract:
            raise CodexGatewayError("contract-drift", "model roles must not be embedded in the CLI contract")
        environment = self.contract.get("environment")
        if not isinstance(environment, dict) or environment.get("allowlist") != list(ENVIRONMENT_KEYS):
            raise CodexGatewayError("contract-drift", "closed environment contract drift")
        status, generation, compatibility = self.contract.get("status"), self.contract.get("generation"), self.contract.get("compatibility_probe")
        template = ["exec", "--ignore-user-config", "--ignore-rules", "--cd", "{empty_cwd}", "--model", "{model}", "-c", 'model_reasoning_effort="{reasoning_effort}"', "--sandbox", "read-only", "--ephemeral", "--json", "--color", "never", "--output-schema", "{output_schema}", "--output-last-message", "{output_last_message}", "-"]
        if status != {"argv": ["login", "status"], "expected_stdout": EXPECTED_AUTH}:
            raise CodexGatewayError("contract-drift", "login status contract drift")
        if generation != {"argv_template": template, "result_source": "output-last-message-only"}:
            raise CodexGatewayError("contract-drift", "generation argv contract drift")
        if compatibility != {"argv": ["exec", "--help"], "required_options": list(REQUIRED_EXEC_OPTIONS)}:
            raise CodexGatewayError("contract-drift", "compatibility probe contract drift")

    def _trusted_install_paths(self) -> tuple[Path, Path]:
        discovery = self.policy["discovery"]
        assert isinstance(discovery, dict)
        primary = Path(_known_folder(28)) / str(discovery["primary_local_appdata_relative"])
        releases = Path(_known_folder(40)) / str(discovery["release_root_userprofile_relative"])
        return primary, releases

    def _resolve_trusted_executable(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise CodexGatewayError("invalid-file", "Codex executable is unavailable") from error
        if path.is_symlink() or not path.is_file() or _has_reparse_point(path) or not _same_path(path.absolute(), resolved):
            raise CodexGatewayError("executable-reparse", "Codex executable escaped its trusted installation path")
        if self.paths.testing:
            if self.paths.attestation.parent.resolve(strict=True) not in resolved.parents:
                raise CodexGatewayError("untrusted-path", "testing Codex executable escaped its isolated root")
            return resolved
        primary, releases = self._trusted_install_paths()
        if _same_path(resolved, primary.resolve(strict=False)):
            return resolved
        try:
            relative = resolved.relative_to(releases.resolve(strict=False))
        except ValueError as error:
            raise CodexGatewayError("untrusted-path", "Codex executable is outside official installation roots") from error
        expected_relative = str(self.policy["discovery"]["release_executable_relative"])
        if len(relative.parts) != 3 or Path(*relative.parts[1:]).as_posix().casefold() != Path(expected_relative).as_posix().casefold():
            raise CodexGatewayError("untrusted-path", "Codex release executable path is invalid")
        return resolved

    def _invoke(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        stdin: bytes = b"",
        cwd: Path | None = None,
        timeout_seconds: int = 300,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="codex-child-", ignore_cleanup_errors=True) as raw_temp:
            temporary = Path(raw_temp)
            child_cwd = cwd or temporary
            request = ProcessRequest((str(executable), *arguments), stdin, child_cwd, _closed_environment(executable, temporary), timeout_seconds)
            try:
                return self.runner.run(request)
            except subprocess.TimeoutExpired as error:
                raise CodexGatewayError("process-timeout", "Codex child exceeded the configured time limit") from error
            except OSError as error:
                raise CodexGatewayError("process-failed", "Codex child could not complete") from error

    def _authenticode(self, executable: Path) -> tuple[str, str]:
        if self.paths.testing:
            return "Valid", next(iter(TRUSTED_OPENAI_SIGNERS))
        script = "$s=Get-AuthenticodeSignature -LiteralPath '" + str(executable).replace("'", "''") + "';@{status=[string]$s.Status;signer=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
        powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        with tempfile.TemporaryDirectory(prefix="codex-signature-") as raw_temp:
            try:
                result = subprocess.run(
                    (str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script),
                    capture_output=True, cwd=raw_temp,
                    env=_closed_environment(executable, Path(raw_temp)), timeout=30, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CodexGatewayError("signature-check", "Authenticode verification failed") from error
        try:
            payload: JsonValue = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CodexGatewayError("signature-check", "Authenticode verification failed") from error
        if result.returncode != 0 or not isinstance(payload, dict):
            raise CodexGatewayError("signature-check", "Authenticode verification failed")
        return str(payload.get("status", "")), str(payload.get("signer", ""))

    def _inspect_identity(self, path: Path) -> JsonObject:
        executable = self._resolve_trusted_executable(path)
        metadata = executable.stat()
        cache_key = (os.path.normcase(str(executable)), metadata.st_size, metadata.st_mtime_ns)
        cached = self._identity_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        # Authenticate and hash bytes before ever executing the candidate.
        status, signer = self._authenticode(executable)
        if status != "Valid" or signer not in TRUSTED_OPENAI_SIGNERS:
            raise CodexGatewayError("signature-drift", "Codex Authenticode identity is not trusted")
        binary_sha256 = _digest(executable)
        version_result = self._invoke(executable, ("--version",), timeout_seconds=30)
        version_text = version_result.stdout.decode(errors="replace").strip()
        match = re.fullmatch(r"codex-cli\s+([^\s]+)", version_text)
        if version_result.returncode != 0 or match is None:
            raise CodexGatewayError("version-invalid", "Codex CLI version output is invalid")
        help_result = self._invoke(executable, ("exec", "--help"), timeout_seconds=30)
        help_text = (help_result.stdout + help_result.stderr).decode(errors="replace")
        if help_result.returncode != 0 or any(option not in help_text for option in REQUIRED_EXEC_OPTIONS):
            raise CodexGatewayError("compatibility-probe", "Codex CLI lacks required isolated generation options")
        identity: JsonObject = {
            "authenticode_status": status,
            "binary_sha256": binary_sha256,
            "cli_version": match.group(1),
            "executable": str(executable),
            "file_size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "signer": signer,
        }
        self._assert_identity_unchanged(executable, identity)
        self._identity_cache[cache_key] = dict(identity)
        return identity

    def _status(self, executable: Path) -> None:
        result = self._invoke(executable, ("login", "status"), timeout_seconds=30)
        stdout = result.stdout.decode(errors="replace").strip()
        stderr = result.stderr.decode(errors="replace").strip()
        exact_channel = (stdout == EXPECTED_AUTH and not stderr) or (stderr == EXPECTED_AUTH and not stdout)
        if result.returncode != 0 or not exact_channel:
            raise CodexGatewayError("auth-status", "saved ChatGPT authentication is unavailable")

    def _assert_identity_unchanged(self, executable: Path, identity: JsonObject) -> None:
        try:
            metadata = executable.stat()
        except OSError as error:
            raise CodexGatewayError("binary-race", "Codex executable changed during execution") from error
        if metadata.st_size != identity.get("file_size") or metadata.st_mtime_ns != identity.get("mtime_ns") or _digest(executable) != identity.get("binary_sha256"):
            raise CodexGatewayError("binary-race", "Codex executable changed during execution")

    def _record_metrics(self, stdout: bytes, duration_seconds: float) -> None:
        metrics: JsonObject = {"duration_seconds": round(duration_seconds, 6)}
        for line in stdout.splitlines():
            try:
                event: JsonValue = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                    value = usage.get(key)
                    if type(value) is int and value >= 0:
                        metrics[key] = value
        self.last_run_metrics = metrics

    def _execute(self, runtime: _Runtime, role: str, prompt: str, schema: JsonObject) -> JsonObject:
        config = self.role_configs.get(role)
        if config is None:
            raise CodexGatewayError("unknown-role", "unsupported Codex generation role")
        model, effort = config["model"], config["reasoning_effort"]
        timeout_seconds = config["timeout_seconds"]
        assert isinstance(model, str) and isinstance(effort, str) and isinstance(timeout_seconds, int)
        # A short-lived Codex helper may retain this directory as its cwd after
        # the direct CLI exits. Cleanup removes files and tolerates that Windows
        # directory lock instead of masking the completed result.
        with tempfile.TemporaryDirectory(prefix="codex-exec-", ignore_cleanup_errors=True) as raw_root:
            root, cwd = Path(raw_root), Path(raw_root) / "empty"
            for relative in ("objects", "refs/heads"):
                (root / ".git" / relative).mkdir(parents=True)
            (root / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
            (root / ".git/config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n", encoding="ascii")
            cwd.mkdir()
            schema_path, result_path = root / "schema.json", root / "result.json"
            schema_path.write_bytes(_canonical(schema))
            arguments = ("exec", "--ignore-user-config", "--ignore-rules", "--cd", str(cwd), "--model", model, "-c", f'model_reasoning_effort="{effort}"', "--sandbox", "read-only", "--ephemeral", "--json", "--color", "never", "--output-schema", str(schema_path), "--output-last-message", str(result_path), "-")
            self._assert_identity_unchanged(runtime.executable, runtime.identity)
            started = time.monotonic()
            self.last_run_metrics = None
            self.last_run_provenance = None
            try:
                result = self._invoke(runtime.executable, arguments, prompt.encode("utf-8"), cwd, timeout_seconds)
            except CodexGatewayError:
                self.last_run_metrics = {"duration_seconds": round(time.monotonic() - started, 6)}
                raise
            self._record_metrics(result.stdout, time.monotonic() - started)
            self._assert_identity_unchanged(runtime.executable, runtime.identity)
            if result.returncode != 0 or not result_path.is_file() or result_path.is_symlink():
                raise CodexGatewayError("generation-failed", "Codex did not publish a final result")
            if result_path.stat().st_size > 1_048_576:
                raise CodexGatewayError("result-limit", "Codex result exceeded the gateway limit")
            value = _load_object(result_path)
            try:
                validate_json(value, schema)
            except SchemaError as error:
                raise CodexGatewayError("schema-invalid", "Codex final result failed schema validation") from error
            contract_hash, policy_hash = self._document_hashes()
            self.last_run_provenance = {
                "binary_sha256": runtime.identity["binary_sha256"],
                "cache_compatibility_epoch": self.contract["cache_compatibility_epoch"],
                "cli_version": runtime.identity["cli_version"],
                "contract_sha256": contract_hash,
                "executable": str(runtime.executable),
                "model": model,
                "policy_sha256": policy_hash,
                "reasoning_effort": effort,
                "role": self._canonical_role(role),
                "role_fingerprint": self.role_fingerprints[role],
            }
            return value

    def _document_hashes(self) -> tuple[str, str]:
        return hashlib.sha256(_canonical(self.contract)).hexdigest(), hashlib.sha256(_canonical(self.policy)).hexdigest()

    def _verify_attestation_document(self) -> JsonObject:
        attestation = _load_object(self.paths.attestation)
        contract_hash, policy_hash = self._document_hashes()
        if (
            attestation.get("schema") != "codex-cli-attestation-v2"
            or attestation.get("schema_version") != 2
            or attestation.get("auth_status") != EXPECTED_AUTH
            or attestation.get("contract_sha256") != contract_hash
            or attestation.get("policy_sha256") != policy_hash
            or attestation.get("cache_compatibility_epoch") != self.contract.get("cache_compatibility_epoch")
            or not isinstance(attestation.get("qualified_routes"), dict)
            or not isinstance(attestation.get("binary_sha256"), str)
            or not isinstance(attestation.get("cli_version"), str)
            or not isinstance(attestation.get("final_executable"), str)
        ):
            raise CodexGatewayError("attestation-drift", "Codex local attestation drift")
        return attestation

    def _attested_runtime(self, role: str, *, require_route: bool = True) -> tuple[_Runtime, JsonObject]:
        attestation = self._verify_attestation_document()
        routes = attestation["qualified_routes"]
        fingerprint = self.role_fingerprints.get(role)
        if require_route and (not isinstance(routes, dict) or not isinstance(fingerprint, str) or fingerprint not in routes):
            raise CodexGatewayError("route-unqualified", "Codex model route has not passed its canary")
        executable = Path(str(attestation["final_executable"]))
        identity = self._inspect_identity(executable)
        for key in ("authenticode_status", "binary_sha256", "cli_version", "file_size", "mtime_ns", "signer"):
            if identity.get(key) != attestation.get(key):
                raise CodexGatewayError("attestation-drift", "Codex last-good executable identity drift")
        return _Runtime(executable, identity), attestation

    def _find_stable_release(self, identity: JsonObject, candidate: Path) -> Path:
        if self.paths.testing:
            stable = self.paths.testing_release
            if stable is not None and _digest(stable) == identity.get("binary_sha256"):
                return self._resolve_trusted_executable(stable)
            return candidate
        _primary, releases = self._trusted_install_paths()
        executable_relative = Path(str(self.policy["discovery"]["release_executable_relative"]))
        matches = sorted((directory / executable_relative for directory in releases.glob("*") if directory.is_dir()), reverse=True)
        for path in matches:
            try:
                resolved = self._resolve_trusted_executable(path)
                metadata = resolved.stat()
                if metadata.st_size == identity.get("file_size") and _digest(resolved) == identity.get("binary_sha256"):
                    return resolved
            except (OSError, CodexGatewayError):
                continue
        return candidate

    @staticmethod
    def _canonical_role(role: str) -> str:
        if role == "long_form":
            return "answer"
        if role == "short_form":
            return "category_summary"
        return role

    def _canary(self, runtime: _Runtime, role: str) -> str:
        canonical_role = self._canonical_role(role)
        schema: JsonObject = {
            "additionalProperties": False,
            "properties": {"role": {"const": canonical_role, "type": "string"}, "status": {"const": "ok", "type": "string"}},
            "required": ["role", "status"],
            "type": "object",
        }
        result = self._execute(runtime, role, f'Return only JSON with role "{canonical_role}" and status "ok".', schema)
        return hashlib.sha256(_canonical(result)).hexdigest()

    def _publish_attestation(self, attestation: JsonObject) -> None:
        self.paths.attestation.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.paths.attestation.parent, delete=False) as handle:
                handle.write(_canonical(attestation))
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, self.paths.attestation)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _qualify_candidate(self, candidate: Path, roles: list[str]) -> tuple[_Runtime, JsonObject]:
        candidate_identity = self._inspect_identity(candidate)
        candidate_runtime = _Runtime(Path(str(candidate_identity["executable"])), candidate_identity)
        self._status(candidate_runtime.executable)
        existing_routes: JsonObject = {}
        try:
            existing = self._verify_attestation_document()
            if existing.get("binary_sha256") == candidate_identity.get("binary_sha256"):
                raw_routes = existing.get("qualified_routes")
                if isinstance(raw_routes, dict):
                    existing_routes = dict(raw_routes)
        except CodexGatewayError:
            pass
        groups: dict[tuple[str, str], list[str]] = {}
        for role in roles:
            config = self.role_configs.get(role)
            if config is None:
                raise CodexGatewayError("unknown-role", "unsupported Codex generation role")
            key = (str(config["model"]), str(config["reasoning_effort"]))
            groups.setdefault(key, []).append(role)
        routes = dict(existing_routes)
        for grouped_roles in groups.values():
            pending = [role for role in grouped_roles if self.role_fingerprints[role] not in routes]
            if not pending:
                continue
            canary_hash = self._canary(candidate_runtime, pending[0])
            for role in pending:
                config = self.role_configs[role]
                routes[self.role_fingerprints[role]] = {
                    "canary_output_sha256": canary_hash,
                    "model": config["model"],
                    "reasoning_effort": config["reasoning_effort"],
                }
        self._assert_identity_unchanged(candidate_runtime.executable, candidate_identity)
        stable = self._find_stable_release(candidate_identity, candidate_runtime.executable)
        stable_identity = self._inspect_identity(stable)
        if stable_identity.get("binary_sha256") != candidate_identity.get("binary_sha256"):
            raise CodexGatewayError("stable-release", "Codex stable release identity mismatch")
        contract_hash, policy_hash = self._document_hashes()
        attestation: JsonObject = {
            **stable_identity,
            "auth_status": EXPECTED_AUTH,
            "cache_compatibility_epoch": self.contract["cache_compatibility_epoch"],
            "contract_sha256": contract_hash,
            "final_executable": str(stable),
            "policy_sha256": policy_hash,
            "qualified_routes": routes,
            "schema": "codex-cli-attestation-v2",
            "schema_version": 2,
        }
        self._publish_attestation(attestation)
        return _Runtime(stable, stable_identity), attestation

    def ensure_ready(self, role: CodexRole | str) -> JsonObject:
        """Qualify a changed official binary for an authorized generation role."""
        role_name = str(role)
        if role_name not in self.role_models:
            raise CodexGatewayError("unknown-role", "unsupported Codex generation role")
        candidate = self.paths.executable
        candidate_error: CodexGatewayError | None = None
        try:
            candidate_identity = self._inspect_identity(candidate)
            runtime, attestation = self._attested_runtime(role_name)
            if candidate_identity.get("binary_sha256") == attestation.get("binary_sha256"):
                self._status(runtime.executable)
                return self.capability_inventory()
            runtime, _attestation = self._qualify_candidate(candidate, [role_name])
            self._status(runtime.executable)
            return self.capability_inventory()
        except CodexGatewayError as error:
            candidate_error = error
        try:
            runtime, _attestation = self._attested_runtime(role_name)
            self._status(runtime.executable)
            return self.capability_inventory()
        except CodexGatewayError:
            pass
        if candidate_error is not None and candidate_error.code not in {"route-unqualified", "attestation-drift", "invalid-json"}:
            raise candidate_error
        runtime, _attestation = self._qualify_candidate(candidate, [role_name])
        self._status(runtime.executable)
        return self.capability_inventory()

    def generate_json(self, role: CodexRole | str, prompt: str, schema: JsonObject) -> JsonObject:
        role_name = str(role)
        _ = self.ensure_ready(role_name)
        runtime, _attestation = self._attested_runtime(role_name)
        self._status(runtime.executable)
        return self._execute(runtime, role_name, prompt, schema)

    def verify_only(self) -> JsonObject:
        """Verify the last-good binary, compatibility, and login without generation."""
        runtime, attestation = self._attested_runtime("answer", require_route=False)
        self._status(runtime.executable)
        return attestation

    def requalify(self, accept: bool, roles: list[str] | None = None) -> JsonObject:
        """Explicit qualification, or a generation-free verification of last-good state."""
        if not accept:
            return self.verify_only()
        selected = roles or list(self.model_config["roles"])
        unknown = [role for role in selected if role not in self.role_models]
        if unknown:
            raise CodexGatewayError("unknown-role", "unsupported Codex generation role")
        _runtime, attestation = self._qualify_candidate(self.paths.executable, selected)
        return attestation

    def capability_inventory(self) -> JsonObject:
        """Read the cached qualification record without child calls or writes."""
        attestation = self._verify_attestation_document()
        raw_routes = attestation["qualified_routes"]
        assert isinstance(raw_routes, dict)
        roles: JsonObject = {}
        qualified_roles: list[JsonValue] = []
        for role, (model, effort) in self.role_models.items():
            roles[role] = {"model": model, "reasoning_effort": effort}
            if self.role_fingerprints[role] in raw_routes:
                qualified_roles.append(role)
        return {
            "attestation_sha256": hashlib.sha256(_canonical(attestation)).hexdigest(),
            "attested": True,
            "binary_sha256": attestation["binary_sha256"],
            "cache_compatibility_epoch": attestation["cache_compatibility_epoch"],
            "cli_version": attestation["cli_version"],
            "contract_sha256": attestation["contract_sha256"],
            "paid_api": False,
            "policy_sha256": attestation["policy_sha256"],
            "provider": "saved-chatgpt-auth-codex-cli",
            "qualified_roles": qualified_roles,
            "roles": roles,
            "schema": "codex-capabilities-v2",
            "schema_version": 2,
        }

    def preflight(self) -> JsonObject:
        """Verify the attested executable and saved login without generation."""
        _ = self.verify_only()
        return self.capability_inventory()
