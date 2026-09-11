"""Project-local CPython 3.12 runtime resolution and qualification.

The published runtime is the last known good environment.  Ambient Python,
the Windows ``py`` launcher, and uv installations are candidates only; none is
used for pipeline work until an explicit qualification has staged, checked, and
published it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


class PythonRuntimeError(RuntimeError):
    """A runtime is absent, untrusted, or incompatible with the policy."""


@dataclass(frozen=True, slots=True)
class PythonRuntime:
    """A verified, project-local interpreter and its immutable identity."""

    root: Path
    executable: Path
    runtime_dir: Path
    attestation_path: Path
    version: str
    trust_kind: str = "unknown"


_POLICY_NAME = "python-runtime-policy-v1.json"
_VERSION_SCRIPT = (
    "import json,platform,sys;"
    "print(json.dumps({'implementation':platform.python_implementation(),"
    "'version':list(sys.version_info[:3]),'executable':sys.executable},sort_keys=True))"
)


def digest(path: Path) -> str:
    """Return a streaming SHA-256 digest for one regular file."""
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def project_root(start: Path | None = None) -> Path:
    """Find the checkout containing the checked-in Python runtime policy."""
    initial = (start or Path(__file__)).resolve()
    for candidate in (initial, *initial.parents):
        if (candidate / "pipeline" / _POLICY_NAME).is_file():
            return candidate
    raise PythonRuntimeError("python-runtime-policy-unavailable")


def load_policy(root: Path | None = None) -> dict[str, object]:
    """Read the small checked-in policy and reject malformed policy files."""
    actual_root = (root or project_root()).resolve()
    path = actual_root / "pipeline" / _POLICY_NAME
    if not path.is_file() or path.is_symlink():
        raise PythonRuntimeError("python-runtime-policy-unavailable")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PythonRuntimeError("python-runtime-policy-invalid") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise PythonRuntimeError("python-runtime-policy-invalid")
    supported = raw.get("supported_python")
    required = (
        "implementation",
        "runtime_dir",
        "attestation_path",
        "managed_runtimes_dir",
        "active_runtime_path",
        "candidate_runtime_path",
        "previous_runtime_path",
        "lock_snapshots_dir",
        "legacy_requirements_lock",
        "qualification_requirements_lock",
        "candidate_environment_variable",
    )
    if (
        not isinstance(supported, dict)
        or supported.get("major") != 3
        or supported.get("minor") != 12
        or any(not isinstance(raw.get(key), str) or not str(raw[key]) for key in required)
    ):
        raise PythonRuntimeError("python-runtime-policy-invalid")
    return raw


def _relative(root: Path, policy: Mapping[str, object], key: str) -> Path:
    value = policy.get(key)
    if not isinstance(value, str):
        raise PythonRuntimeError("python-runtime-policy-invalid")
    candidate = (root / value).resolve()
    if root.resolve() not in candidate.parents:
        raise PythonRuntimeError("python-runtime-policy-invalid")
    return candidate


def _pointer_value(runtime_dir: Path, attestation_path: Path, root: Path) -> dict[str, object]:
    actual_root = root.resolve()
    return {
        "schema_version": 1,
        "runtime_dir": runtime_dir.resolve().relative_to(actual_root).as_posix(),
        "attestation_path": attestation_path.resolve().relative_to(actual_root).as_posix(),
    }


def _write_atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json(value))
    os.replace(temporary, path)


def _runtime_from_pointer(root: Path, policy: Mapping[str, object], pointer_path: Path) -> tuple[Path, Path]:
    pointer = _safe_json(pointer_path)
    if pointer.get("schema_version") != 1:
        raise PythonRuntimeError("python-runtime-pointer-invalid")
    raw_runtime = pointer.get("runtime_dir")
    raw_attestation = pointer.get("attestation_path")
    if not isinstance(raw_runtime, str) or not isinstance(raw_attestation, str):
        raise PythonRuntimeError("python-runtime-pointer-invalid")
    runtime_dir = (root / raw_runtime).resolve()
    attestation_path = (root / raw_attestation).resolve()
    legacy = _relative(root, policy, "runtime_dir")
    managed = _relative(root, policy, "managed_runtimes_dir")
    runtime_allowed = runtime_dir == legacy or managed in runtime_dir.parents
    runtime_root = root.resolve()
    if (
        not runtime_allowed
        or runtime_root not in attestation_path.parents
        or attestation_path.parent != _relative(root, policy, "attestation_path").parent
    ):
        raise PythonRuntimeError("python-runtime-pointer-invalid")
    return runtime_dir, attestation_path


def _selected_runtime_paths(root: Path, policy: Mapping[str, object]) -> tuple[Path, Path]:
    active = _relative(root, policy, "active_runtime_path")
    if active.exists():
        return _runtime_from_pointer(root, policy, active)
    return _relative(root, policy, "runtime_dir"), _relative(root, policy, "attestation_path")


def _runtime_executable(runtime_dir: Path) -> Path:
    return runtime_dir / ("Scripts/python.exe" if (runtime_dir / "Scripts/python.exe").is_file() else "python.exe")


def _require_regular(path: Path, code: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise PythonRuntimeError(code)


def _safe_json(path: Path) -> dict[str, object]:
    _require_regular(path, "python-runtime-attestation-invalid")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PythonRuntimeError("python-runtime-attestation-invalid") from error
    if not isinstance(raw, dict):
        raise PythonRuntimeError("python-runtime-attestation-invalid")
    return raw


def _run_json(executable: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", _VERSION_SCRIPT],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env={"PYTHONUTF8": "1", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PythonRuntimeError("python-runtime-interpreter-unavailable") from error
    if result.returncode != 0:
        raise PythonRuntimeError("python-runtime-interpreter-unavailable")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PythonRuntimeError("python-runtime-interpreter-invalid") from error
    if not isinstance(value, dict):
        raise PythonRuntimeError("python-runtime-interpreter-invalid")
    return value


def _version_string(value: object) -> str:
    if not isinstance(value, list) or len(value) != 3 or not all(isinstance(part, int) for part in value):
        raise PythonRuntimeError("python-runtime-interpreter-invalid")
    return ".".join(str(part) for part in value)


def _require_supported_interpreter(executable: Path, policy: Mapping[str, object]) -> str:
    _require_regular(executable, "python-runtime-interpreter-unavailable")
    reported = _run_json(executable)
    supported = policy["supported_python"]
    assert isinstance(supported, dict)
    version = reported.get("version")
    if (
        reported.get("implementation") != policy["implementation"]
        or not isinstance(version, list)
        or version[:2] != [supported["major"], supported["minor"]]
    ):
        raise PythonRuntimeError("python-runtime-unsupported-python")
    reported_path = reported.get("executable")
    if not isinstance(reported_path, str) or os.path.normcase(str(Path(reported_path).resolve())) != os.path.normcase(str(executable.resolve())):
        raise PythonRuntimeError("python-runtime-interpreter-invalid")
    return _version_string(version)


_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s;]+)")


def locked_packages(lock_path: Path) -> dict[str, str]:
    """Parse a fully pinned, hash-bearing lock without evaluating it."""
    _require_regular(lock_path, "python-runtime-lock-unavailable")
    packages: dict[str, str] = {}
    hashes: dict[str, bool] = {}
    active: str | None = None
    for raw in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = _PIN.match(line)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if active is not None and not hashes.get(active, False):
                raise PythonRuntimeError("python-runtime-lock-incomplete")
            packages[name] = match.group(2)
            hashes[name] = "--hash=sha256:" in line
            active = name
        elif "--hash=sha256:" in line and active is not None:
            hashes[active] = True
    if not packages or active is None or not all(hashes.get(name, False) for name in packages):
        raise PythonRuntimeError("python-runtime-lock-incomplete")
    return packages


def _attest_package_files(runtime_dir: Path) -> list[dict[str, object]]:
    site = runtime_dir / "Lib" / "site-packages"
    if not site.is_dir() or site.is_symlink():
        raise PythonRuntimeError("python-runtime-site-packages-unavailable")
    result: list[dict[str, object]] = []
    for item in sorted(site.rglob("*"), key=lambda path: path.as_posix().encode("utf-8")):
        if item.is_file() and not item.is_symlink():
            result.append({
                "path": item.relative_to(runtime_dir).as_posix(),
                "size": item.stat().st_size,
                "sha256": digest(item),
            })
    if not result:
        raise PythonRuntimeError("python-runtime-site-packages-unavailable")
    return result


def make_attestation(runtime_dir: Path, executable: Path, lock_path: Path, policy: Mapping[str, object], root: Path) -> dict[str, object]:
    """Create the attestation only after the staged runtime is usable."""
    version = _require_supported_interpreter(executable, policy)
    packages = locked_packages(lock_path)
    return {
        "schema_version": 2,
        "runtime_type": "venv",
        "implementation": policy["implementation"],
        "python_version": version,
        "python_executable_sha256": digest(executable),
        "requirements_lock": lock_path.resolve().relative_to(root.resolve()).as_posix(),
        "requirements_sha256": digest(lock_path),
        "locked_packages": packages,
        "package_files": _attest_package_files(runtime_dir),
    }


def _validate_attestation_files(runtime_dir: Path, attestation: Mapping[str, object], *, verify_files: bool) -> None:
    entries = attestation.get("package_files")
    if not isinstance(entries, list) or not entries:
        raise PythonRuntimeError("python-runtime-dependency-identity-invalid")
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise PythonRuntimeError("python-runtime-dependency-identity-invalid")
        relative, expected_size, expected_hash = raw.get("path"), raw.get("size"), raw.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_size, int) or not isinstance(expected_hash, str):
            raise PythonRuntimeError("python-runtime-dependency-identity-invalid")
        safe_relative = PurePosixPath(relative)
        if (
            safe_relative.is_absolute()
            or ".." in safe_relative.parts
            or safe_relative.parts[:2] != ("Lib", "site-packages")
            or relative in seen
            or len(expected_hash) != 64
        ):
            raise PythonRuntimeError("python-runtime-dependency-identity-invalid")
        seen.add(relative)
        if verify_files:
            # ``safe_relative`` was checked before concatenation, so this avoids
            # thousands of costly filesystem resolves during a doctor scan.
            candidate = runtime_dir.joinpath(*safe_relative.parts)
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != expected_size
                or digest(candidate) != expected_hash
            ):
                raise PythonRuntimeError("python-runtime-dependency-identity-invalid")


def _attested_lock_path(root: Path, policy: Mapping[str, object], lock_name: str) -> Path:
    """Resolve only checked-in locks or immutable per-runtime lock snapshots."""
    legacy = str(policy["legacy_requirements_lock"])
    qualification = str(policy["qualification_requirements_lock"])
    if lock_name in {legacy, qualification}:
        return root / lock_name
    snapshots = _relative(root, policy, "lock_snapshots_dir")
    candidate = (root / lock_name).resolve()
    if snapshots not in candidate.parents or candidate.parent != snapshots:
        raise PythonRuntimeError("python-runtime-lock-invalid")
    if not re.fullmatch(r"sha256-[0-9a-f]{64}\.txt", candidate.name):
        raise PythonRuntimeError("python-runtime-lock-invalid")
    return candidate


def _snapshot_lock(root: Path, policy: Mapping[str, object], lock: Path) -> Path:
    """Publish a content-addressed copy before a candidate is attested.

    Future edits to the checked-in qualification lock therefore cannot weaken or
    invalidate an already-published last-known-good runtime.
    """
    _require_regular(lock, "python-runtime-lock-unavailable")
    content = lock.read_bytes()
    value = hashlib.sha256(content).hexdigest()
    snapshots = _relative(root, policy, "lock_snapshots_dir")
    target = snapshots / f"sha256-{value}.txt"
    if target.exists():
        _require_regular(target, "python-runtime-lock-invalid")
        if target.read_bytes() != content:
            raise PythonRuntimeError("python-runtime-lock-identity-invalid")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
    return target


def _validate_lock_identity(root: Path, attestation: Mapping[str, object], policy: Mapping[str, object]) -> None:
    lock_name = attestation.get("requirements_lock")
    if not isinstance(lock_name, str):
        # Schema v1 recorded only the legacy lock hash.
        lock_name = str(policy["legacy_requirements_lock"])
    lock = _attested_lock_path(root, policy, lock_name)
    _require_regular(lock, "python-runtime-lock-unavailable")
    expected = attestation.get("requirements_sha256")
    if not isinstance(expected, str) or digest(lock) != expected:
        raise PythonRuntimeError("python-runtime-lock-identity-invalid")
    if lock.parent == _relative(root, policy, "lock_snapshots_dir") and lock.name != f"sha256-{expected}.txt":
        raise PythonRuntimeError("python-runtime-lock-identity-invalid")
    # A new qualified runtime must originate from a closed, hash-bearing lock.
    if attestation.get("schema_version") == 2:
        parsed = locked_packages(lock)
        recorded = attestation.get("locked_packages")
        if recorded != parsed:
            raise PythonRuntimeError("python-runtime-lock-identity-invalid")


def validate_runtime(root: Path | None = None, *, verify_files: bool = True) -> PythonRuntime:
    """Verify interpreter, policy/lock binding, and every attested package file."""
    actual_root = (root or project_root()).resolve()
    policy = load_policy(actual_root)
    runtime_dir, attestation_path = _selected_runtime_paths(actual_root, policy)
    executable = _runtime_executable(runtime_dir)
    attestation = _safe_json(attestation_path)
    version = _require_supported_interpreter(executable, policy)
    if attestation.get("python_version") != version:
        raise PythonRuntimeError("python-runtime-version-identity-invalid")
    expected_executable = attestation.get("python_executable_sha256")
    if not isinstance(expected_executable, str) or digest(executable) != expected_executable:
        raise PythonRuntimeError("python-runtime-executable-identity-invalid")
    _validate_lock_identity(actual_root, attestation, policy)
    _validate_attestation_files(runtime_dir, attestation, verify_files=verify_files)
    # Legacy embedded runtimes additionally pin their stdlib bootstrap files.
    for filename, key in (("python312._pth", "pth_sha256"), ("python312.zip", "stdlib_sha256")):
        expected = attestation.get(key)
        if expected is not None:
            candidate = runtime_dir / filename
            if not isinstance(expected, str) or not candidate.is_file() or candidate.is_symlink() or digest(candidate) != expected:
                raise PythonRuntimeError("python-runtime-bootstrap-identity-invalid")
    trust_kind = "qualified" if attestation.get("schema_version") == 2 else "legacy-attested"
    return PythonRuntime(actual_root, executable, runtime_dir, attestation_path, version, trust_kind)


def resolve_runtime(root: Path | None = None) -> PythonRuntime:
    """Resolve the published runtime for launch without ambient-tool fallback.

    Entry points still verify the executable and lock binding.  ``doctor``
    invokes :func:`validate_runtime` with the default complete file attestation
    before reporting the installation ready.
    """
    return validate_runtime(root, verify_files=False)


def discover_candidates(root: Path | None = None) -> tuple[Path, ...]:
    """List plausible CPython 3.12 candidates for explicit qualification.

    This function never invokes candidates and deliberately does not call the
    Windows ``py`` launcher, whose registration can point at a newer Python.
    """
    actual_root = (root or project_root()).resolve()
    policy = load_policy(actual_root)
    paths: list[Path] = []
    explicit = os.environ.get(str(policy["candidate_environment_variable"]), "").strip()
    if explicit:
        paths.append(Path(explicit))
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        uv_root = Path(appdata) / "uv" / "python"
        paths.extend(sorted(uv_root.glob("cpython-3.12-*/python.exe"), reverse=True))
    found = shutil.which("python3.12")
    if found:
        paths.append(Path(found))
    if sys.version_info[:2] == (3, 12):
        paths.append(Path(sys.executable))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in paths:
        key = os.path.normcase(str(candidate.resolve()))
        if key not in seen and candidate.is_file() and not candidate.is_symlink():
            seen.add(key)
            unique.append(candidate.resolve())
    return tuple(unique)


def _clean_environment() -> dict[str, str]:
    return {
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONUTF8": "1",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PATH": os.environ.get("SYSTEMROOT", "") + os.pathsep + str(Path(os.environ.get("SYSTEMROOT", "")) / "System32"),
    }


def _run_checked(argv: list[str], *, cwd: Path) -> None:
    try:
        result = subprocess.run(argv, cwd=cwd, env=_clean_environment(), capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise PythonRuntimeError("python-runtime-qualification-command-failed") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-800:].strip()
        raise PythonRuntimeError(f"python-runtime-qualification-failed: {detail or 'command failed'}")


def _candidate_paths(root: Path, policy: Mapping[str, object]) -> tuple[Path, Path]:
    """Return the candidate pointer and the managed runtime root."""
    return (
        _relative(root, policy, "candidate_runtime_path"),
        _relative(root, policy, "managed_runtimes_dir"),
    )


def stage_candidate(candidate: Path, root: Path | None = None) -> PythonRuntime:
    """Build and fully attest a candidate without changing the active runtime."""
    actual_root = (root or project_root()).resolve()
    policy = load_policy(actual_root)
    candidate = candidate.resolve()
    _ = _require_supported_interpreter(candidate, policy)
    candidate_pointer, managed_root = _candidate_paths(actual_root, policy)
    source_lock = actual_root / str(policy["qualification_requirements_lock"])
    _ = locked_packages(source_lock)
    lock = _snapshot_lock(actual_root, policy, source_lock)
    if candidate_pointer.exists():
        raise PythonRuntimeError("python-runtime-candidate-already-staged")
    version = _require_supported_interpreter(candidate, policy)
    token = uuid.uuid4().hex
    managed_root.mkdir(parents=True, exist_ok=True)
    runtime_dir = managed_root / f"cpython-{version}-{token}"
    attestation_path = _relative(actual_root, policy, "attestation_path").with_name(f"python312-resolved-{token}.json")
    try:
        # The venv is created at its final unique path.  Promotion switches only
        # a small JSON pointer, so a running legacy interpreter is never moved.
        _run_checked([str(candidate), "-I", "-m", "venv", str(runtime_dir)], cwd=actual_root)
        executable = _runtime_executable(runtime_dir)
        _run_checked([str(executable), "-I", "-m", "pip", "install", "--isolated", "--require-hashes", "--no-deps", "-r", str(lock)], cwd=actual_root)
        site_packages = runtime_dir / "Lib" / "site-packages"
        if not site_packages.is_dir():
            raise PythonRuntimeError("python-runtime-site-packages-unavailable")
        # ``-I`` deliberately ignores the cwd.  Keep the repository importable
        # through the venv itself, using a path relative to this managed runtime.
        relative_root = os.path.relpath(actual_root, site_packages)
        (site_packages / "kaic-paper-curation-repository.pth").write_text(relative_root + "\n", encoding="utf-8")
        _run_checked([str(executable), "-I", "-m", "pip", "check"], cwd=actual_root)
        _run_checked([str(executable), "-I", "-c",
                      "import adapters, fitz, numpy, pandas, sklearn, umap, hdbscan; import pipeline"], cwd=actual_root)
        payload = make_attestation(runtime_dir, executable, lock, policy, actual_root)
        _write_atomic_json(attestation_path, payload)
        verified = validate_runtime_from_paths(actual_root, runtime_dir, attestation_path, policy)
        _write_atomic_json(candidate_pointer, _pointer_value(runtime_dir, attestation_path, actual_root))
        return PythonRuntime(actual_root, executable, runtime_dir, attestation_path, verified.version, verified.trust_kind)
    except BaseException:
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        if attestation_path.exists():
            attestation_path.unlink()
        raise


def promote_staged_candidate(root: Path | None = None) -> PythonRuntime:
    """Atomically make the already-attested candidate active and keep prior LKG."""
    actual_root = (root or project_root()).resolve()
    policy = load_policy(actual_root)
    candidate_pointer, _managed_root = _candidate_paths(actual_root, policy)
    runtime_dir, attestation_path = _runtime_from_pointer(actual_root, policy, candidate_pointer)
    verified = validate_runtime_from_paths(actual_root, runtime_dir, attestation_path, policy)
    active_pointer = _relative(actual_root, policy, "active_runtime_path")
    previous_pointer = _relative(actual_root, policy, "previous_runtime_path")
    if active_pointer.exists():
        old_value = _safe_json(active_pointer)
    else:
        old_value = _pointer_value(
            _relative(actual_root, policy, "runtime_dir"),
            _relative(actual_root, policy, "attestation_path"),
            actual_root,
        )
    _write_atomic_json(previous_pointer, old_value)
    _write_atomic_json(active_pointer, _pointer_value(runtime_dir, attestation_path, actual_root))
    candidate_pointer.unlink()
    # Complete verification after publish is part of the promotion contract.
    return validate_runtime(actual_root, verify_files=True)


def rollback_previous_runtime(root: Path | None = None) -> PythonRuntime:
    """Atomically point future launches back to the retained previous runtime."""
    actual_root = (root or project_root()).resolve()
    policy = load_policy(actual_root)
    previous_pointer = _relative(actual_root, policy, "previous_runtime_path")
    active_pointer = _relative(actual_root, policy, "active_runtime_path")
    previous_runtime, previous_attestation = _runtime_from_pointer(actual_root, policy, previous_pointer)
    _ = validate_runtime_from_paths(actual_root, previous_runtime, previous_attestation, policy)
    current = _safe_json(active_pointer) if active_pointer.exists() else _pointer_value(
        _relative(actual_root, policy, "runtime_dir"),
        _relative(actual_root, policy, "attestation_path"),
        actual_root,
    )
    _write_atomic_json(active_pointer, _safe_json(previous_pointer))
    _write_atomic_json(previous_pointer, current)
    return validate_runtime(actual_root, verify_files=True)


def qualify_candidate(candidate: Path, root: Path | None = None) -> PythonRuntime:
    """Create, install, attest, and publish a candidate only after it passes.

    This is intentionally explicit.  It can download packages permitted by the
    checked-in fully pinned lock, but it never selects a newer Python itself.
    """
    actual_root = (root or project_root()).resolve()
    try:
        _ = stage_candidate(candidate, actual_root)
        return promote_staged_candidate(actual_root)
    except BaseException:
        # A staged failure is intentionally retained only when it already
        # completed validation; otherwise no partial candidate survives.
        raise


def validate_runtime_from_paths(root: Path, runtime_dir: Path, attestation_path: Path, policy: Mapping[str, object] | None = None) -> PythonRuntime:
    """Internal/public test hook for validating an un-published staged pair."""
    active_policy = dict(policy or load_policy(root))
    executable = _runtime_executable(runtime_dir)
    attestation = _safe_json(attestation_path)
    version = _require_supported_interpreter(executable, active_policy)
    if attestation.get("python_version") != version:
        raise PythonRuntimeError("python-runtime-version-identity-invalid")
    if digest(executable) != attestation.get("python_executable_sha256"):
        raise PythonRuntimeError("python-runtime-executable-identity-invalid")
    _validate_lock_identity(root, attestation, active_policy)
    _validate_attestation_files(runtime_dir, attestation, verify_files=True)
    trust_kind = "qualified" if attestation.get("schema_version") == 2 else "legacy-attested"
    return PythonRuntime(root, executable, runtime_dir, attestation_path, version, trust_kind)
