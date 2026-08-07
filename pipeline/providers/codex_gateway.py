"""Fail-closed saved-ChatGPT-auth Codex CLI generation boundary."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias

from pipeline.schemas.codex_schema import JsonObject, JsonValue, SchemaError, validate_json


CodexRole: TypeAlias = Literal["long_form", "short_form"]
EXPECTED_VERSION: Final = "0.146.1"
EXPECTED_POLICY_CONTENT_SHA256: Final = "247ddff6dec6f71bc0a93ca73dc3378ff10a296314ad29332c19487c3e8deead"
EXPECTED_AUTH: Final = "Logged in using ChatGPT"
ROLE_MODELS: Final[dict[CodexRole, tuple[str, str]]] = {
    "long_form": ("gpt-5.6-terra", "xhigh"),
    "short_form": ("gpt-5.6-luna", "xhigh"),
}
ENVIRONMENT_KEYS: Final = (
    "SystemRoot", "WINDIR", "ComSpec", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PATH", "LANG", "LC_ALL",
)


@dataclass(frozen=True, slots=True)
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
    def run(self, request: ProcessRequest) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            request.argv, input=request.stdin, cwd=request.cwd, env=request.environment,
            capture_output=True, timeout=request.timeout_seconds, check=False,
        )


@dataclass(frozen=True, slots=True)
class GatewayPaths:
    repository: Path
    executable: Path
    attestation: Path
    testing: bool = False


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


def _resolve_approved_executable(path: Path, approved: Path) -> Path:
    resolved = path.resolve(strict=True)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(approved)) or resolved.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise CodexGatewayError("executable-reparse", "canonical executable escaped its approved release target")
    return resolved


class CodexGateway:
    """Validate one signed Codex binary and consume only its final result file."""

    def __init__(self, paths: GatewayPaths, runner: ProcessRunner | None = None) -> None:
        self.paths = paths
        self.runner = runner or SubprocessRunner()
        self.contract_path = paths.repository / "pipeline/codex-cli-contract.json"
        self.policy_path = paths.repository / "pipeline/codex-cli-policy.json"
        self.contract, self.policy = _load_object(self.contract_path), _load_object(self.policy_path)
        self._validate_checked_contract()

    @classmethod
    def production(cls, repository: Path | None = None) -> "CodexGateway":
        root = (repository or Path(__file__).resolve().parents[2]).resolve()
        policy = _load_object(root / "pipeline/codex-cli-policy.json")
        executable, attestation = policy.get("canonical_executable"), policy.get("attestation_path")
        if not isinstance(executable, str) or not isinstance(attestation, str):
            raise CodexGatewayError("policy-drift", "canonical policy paths are invalid")
        return cls(GatewayPaths(root, Path(executable), root / attestation))

    @classmethod
    def for_testing(cls, paths: GatewayPaths, runner: ProcessRunner) -> "CodexGateway":
        resolved_root = paths.attestation.parent.resolve(strict=True)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        if os.environ.get("PAPER_CURATION_TESTING") != "1" or temp_root not in resolved_root.parents:
            raise CodexGatewayError("test-override", "testing override requires a unique temporary root")
        resolved_executable = paths.executable.resolve(strict=True)
        if resolved_root not in resolved_executable.parents:
            raise CodexGatewayError("test-override", "testing executable must be inside its unique root")
        return cls(GatewayPaths(paths.repository.resolve(), resolved_executable, paths.attestation.resolve(), True), runner)

    def _validate_checked_contract(self) -> None:
        if hashlib.sha256(_canonical(self.policy)).hexdigest() != EXPECTED_POLICY_CONTENT_SHA256:
            raise CodexGatewayError("policy-drift", "canonical Codex policy content drift")
        if self.policy.get("cli_version") != EXPECTED_VERSION or self.policy.get("authenticode_status") != "Valid":
            raise CodexGatewayError("policy-drift", "version or signer policy drift")
        if self.contract.get("roles") != {name: {"model": model, "reasoning_effort": effort} for name, (model, effort) in ROLE_MODELS.items()}:
            raise CodexGatewayError("contract-drift", "central role contract drift")
        environment = self.contract.get("environment")
        if not isinstance(environment, dict) or environment.get("allowlist") != list(ENVIRONMENT_KEYS):
            raise CodexGatewayError("contract-drift", "closed environment contract drift")
        status, generation = self.contract.get("status"), self.contract.get("generation")
        expected_template = ["exec", "--ignore-user-config", "--ignore-rules", "--cd", "{empty_cwd}", "--model", "{model}", "-c", 'model_reasoning_effort="{reasoning_effort}"', "--sandbox", "read-only", "--ephemeral", "--json", "--color", "never", "--output-schema", "{output_schema}", "--output-last-message", "{output_last_message}", "-"]
        if not isinstance(status, dict) or status != {"argv": ["login", "status"], "expected_stdout": EXPECTED_AUTH}:
            raise CodexGatewayError("contract-drift", "login status contract drift")
        if not isinstance(generation, dict) or generation != {"argv_template": expected_template, "result_source": "output-last-message-only"}:
            raise CodexGatewayError("contract-drift", "generation argv contract drift")

    def _invoke(self, arguments: tuple[str, ...], stdin: bytes = b"", cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="codex-child-") as raw_temp:
            temporary = Path(raw_temp)
            child_cwd = cwd or temporary
            request = ProcessRequest((str(self.paths.executable), *arguments), stdin, child_cwd, _closed_environment(self.paths.executable, temporary), 300)
            try:
                return self.runner.run(request)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CodexGatewayError("process-failed", "Codex child could not complete") from error

    def _identity(self) -> JsonObject:
        approved_final, approved_signer = self.policy.get("canonical_final_executable"), self.policy.get("signer")
        if not isinstance(approved_final, str) or not isinstance(approved_signer, str):
            raise CodexGatewayError("policy-drift", "canonical executable identity is invalid")
        final_executable = self.paths.executable if self.paths.testing else _resolve_approved_executable(self.paths.executable, Path(approved_final))
        version = self._invoke(("--version",))
        expected = f"codex-cli {EXPECTED_VERSION}"
        if version.returncode != 0 or version.stdout.decode(errors="replace").strip() != expected:
            raise CodexGatewayError("version-drift", "Codex CLI version drift")
        status, signer = ("Valid", approved_signer) if self.paths.testing else self._authenticode()
        if status != "Valid" or signer != approved_signer:
            raise CodexGatewayError("signature-drift", "Codex Authenticode identity drift")
        return {"authenticode_status": status, "binary_sha256": _digest(self.paths.executable), "cli_version": EXPECTED_VERSION, "executable": str(self.paths.executable), "final_executable": str(final_executable), "signer": signer}

    def _authenticode(self) -> tuple[str, str]:
        script = "$s=Get-AuthenticodeSignature -LiteralPath '" + str(self.paths.executable).replace("'", "''") + "';@{status=[string]$s.Status;signer=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
        powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        with tempfile.TemporaryDirectory(prefix="codex-signature-") as raw_temp:
            try:
                result = subprocess.run(
                    (str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script),
                    capture_output=True, cwd=raw_temp,
                    env=_closed_environment(self.paths.executable, Path(raw_temp)), timeout=30, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CodexGatewayError("signature-check", "Authenticode verification failed") from error
        if result.returncode != 0:
            raise CodexGatewayError("signature-check", "Authenticode verification failed")
        try:
            payload: JsonValue = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CodexGatewayError("signature-check", "Authenticode verification failed") from error
        if not isinstance(payload, dict):
            raise CodexGatewayError("signature-check", "Authenticode verification failed")
        return str(payload.get("status", "")), str(payload.get("signer", ""))

    def _status(self) -> None:
        result = self._invoke(("login", "status"))
        if result.returncode != 0 or result.stdout or result.stderr.decode(errors="replace").strip() != EXPECTED_AUTH:
            raise CodexGatewayError("auth-status", "saved ChatGPT authentication is unavailable")

    def _execute(self, role: CodexRole, prompt: str, schema: JsonObject) -> JsonObject:
        model, effort = ROLE_MODELS[role]
        with tempfile.TemporaryDirectory(prefix="codex-exec-") as raw_root:
            root, cwd = Path(raw_root), Path(raw_root) / "empty"
            for relative in ("objects", "refs/heads"):
                (root / ".git" / relative).mkdir(parents=True)
            (root / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
            (root / ".git/config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n", encoding="ascii")
            cwd.mkdir()
            schema_path, result_path = root / "schema.json", root / "result.json"
            schema_path.write_bytes(_canonical(schema))
            arguments = ("exec", "--ignore-user-config", "--ignore-rules", "--cd", str(cwd), "--model", model, "-c", f'model_reasoning_effort="{effort}"', "--sandbox", "read-only", "--ephemeral", "--json", "--color", "never", "--output-schema", str(schema_path), "--output-last-message", str(result_path), "-")
            result = self._invoke(arguments, prompt.encode("utf-8"), cwd)
            if result.returncode != 0 or not result_path.is_file() or result_path.is_symlink():
                raise CodexGatewayError("generation-failed", "Codex did not publish a final result")
            if result_path.stat().st_size > 1_048_576:
                raise CodexGatewayError("result-limit", "Codex result exceeded the gateway limit")
            value = _load_object(result_path)
            try:
                validate_json(value, schema)
            except SchemaError as error:
                raise CodexGatewayError("schema-invalid", "Codex final result failed schema validation") from error
            return value

    def _expected_attestation(self, canaries: JsonObject | None = None) -> JsonObject:
        return {**self._identity(), "auth_status": EXPECTED_AUTH, "canary_output_sha256": canaries or {}, "contract_sha256": hashlib.sha256(_canonical(self.contract)).hexdigest(), "policy_sha256": hashlib.sha256(_canonical(self.policy)).hexdigest(), "roles": self.contract["roles"], "schema": "codex-cli-attestation-v1", "schema_version": 1}

    def _verify_attestation(self) -> JsonObject:
        attestation = _load_object(self.paths.attestation)
        raw_canaries = attestation.get("canary_output_sha256")
        if not isinstance(raw_canaries, dict) or not all(isinstance(name, str) and isinstance(value, str) for name, value in raw_canaries.items()):
            raise CodexGatewayError("attestation-drift", "Codex local attestation drift")
        canaries: JsonObject = {name: value for name, value in raw_canaries.items() if isinstance(name, str) and isinstance(value, str)}
        expected = self._expected_attestation(canaries)
        if attestation != expected:
            raise CodexGatewayError("attestation-drift", "Codex local attestation drift")
        return attestation

    def generate_json(self, role: CodexRole, prompt: str, schema: JsonObject) -> JsonObject:
        self._verify_attestation()
        self._status()
        return self._execute(role, prompt, schema)

    def requalify(self, accept: bool) -> JsonObject:
        trusted: JsonObject | None = None
        if not accept:
            trusted = self._verify_attestation()
        self._status()
        schema = _load_object(self.paths.repository / "pipeline/schemas/codex-canary-v1.json")
        outputs = {role: self._execute(role, f'Return only JSON with role "{role}" and status "ok".', schema) for role in ROLE_MODELS}
        hashes: JsonObject = {role: hashlib.sha256(_canonical(value)).hexdigest() for role, value in outputs.items()}
        attestation = self._expected_attestation(hashes)
        if trusted is not None and attestation != trusted:
            raise CodexGatewayError("canary-drift", "Codex canary output drift")
        if accept:
            self.paths.attestation.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.paths.attestation.parent, delete=False) as handle:
                handle.write(_canonical(attestation))
                temporary = Path(handle.name)
            os.replace(temporary, self.paths.attestation)
        return attestation

    def capability_inventory(self) -> JsonObject:
        attestation = self._verify_attestation()
        return {"attested": True, "cli_version": attestation["cli_version"], "contract_sha256": attestation["contract_sha256"], "paid_api": False, "policy_sha256": attestation["policy_sha256"], "provider": "saved-chatgpt-auth-codex-cli", "roles": attestation["roles"], "schema": "codex-capabilities-v1", "schema_version": 1}

    def preflight(self) -> JsonObject:
        """Verify the attested executable and saved login without generation."""
        inventory = self.capability_inventory()
        self._status()
        return inventory
