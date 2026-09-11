#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
"""Verify or explicitly qualify the repository-local CPython 3.12 runtime.

The legacy frozen-archive command remains for recovery of the original 3.12.10
bundle.  Normal maintenance uses ``--check-only`` and, after an operator has
selected a candidate, ``--qualify-candidate``.  No command chooses or downloads
a newer Python on its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Final


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.python_runtime import (  # noqa: E402
    PythonRuntimeError,
    discover_candidates,
    promote_staged_candidate,
    qualify_candidate,
    rollback_previous_runtime,
    stage_candidate,
    validate_runtime,
)


ARCHIVE_SIZE: Final = 11_133_606
ARCHIVE_SHA256: Final = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
PYTHON_SHA256: Final = "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a"
STDLIB_SHA256: Final = "fb131c0ef7e35cc5250a74c8cd18744bf4115fb8163710711f3758d7df3d1f88"
SOURCE_PTH_SHA256: Final = "2820f241bc9d6810d4db21c21cca3845799367fbdf0199620fb37c86a74b945c"
PIP_SIZE: Final = 1_825_227
PIP_SHA256: Final = "2913a38a2abf4ea6b64ab507bd9e967f3b53dc1ede74b01b0931e1ce548751af"
RUNTIME_PTH: Final = b"python312.zip\n.\nLib\\site-packages\nimport site\n"


class ProvisionError(RuntimeError):
    """A frozen runtime input or publication contract failed."""


def digest(path: Path) -> str:
    """Return a streaming SHA-256 digest."""
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def require_file(path: Path, size: int, expected_hash: str) -> None:
    """Reject a missing, reparse, wrong-size, or wrong-hash input."""
    if not path.is_file() or path.is_symlink():
        raise ProvisionError(f"regular file required: {path}")
    if path.stat().st_size != size or digest(path) != expected_hash:
        raise ProvisionError(f"frozen input drift: {path}")


def extract_zip(archive: Path, target: Path) -> None:
    """Extract an archive after rejecting path traversal entries."""
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            destination = (target / member.filename).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise ProvisionError(f"archive path escape: {member.filename}")
        bundle.extractall(target)


def write_create_new(path: Path, payload: bytes) -> None:
    """Publish one immutable artifact without replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def attest(stage: Path, requirements: Path) -> dict[str, int | str | list[dict[str, int | str]]]:
    """Build the deterministic runtime attestation."""
    site = stage / "Lib/site-packages"
    files = [
        {"path": item.relative_to(stage).as_posix(), "size": item.stat().st_size, "sha256": digest(item)}
        for item in sorted(site.rglob("*"), key=lambda path: path.as_posix().encode("utf-8"))
        if item.is_file()
    ]
    return {
        "schema_version": 1,
        "python_version": "3.12.10",
        "python_executable_sha256": digest(stage / "python.exe"),
        "stdlib_sha256": digest(stage / "python312.zip"),
        "pth_sha256": digest(stage / "python312._pth"),
        "pip_version": "25.1.1",
        "requirements_sha256": digest(requirements),
        "package_files": files,
    }


def provision(archive: Path, target: Path, pip_wheel: Path, requirements: Path, json_out: Path) -> None:
    """Verify, stage, install, attest, and atomically publish the runtime."""
    if target.exists() or json_out.exists():
        raise ProvisionError("target and json-out must not exist")
    require_file(archive, ARCHIVE_SIZE, ARCHIVE_SHA256)
    require_file(pip_wheel, PIP_SIZE, PIP_SHA256)
    if not requirements.is_file() or requirements.is_symlink():
        raise ProvisionError("requirements lock must be a regular file")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        extract_zip(archive, stage)
        require_file(stage / "python.exe", 104_952, PYTHON_SHA256)
        require_file(stage / "python312.zip", 3_835_482, STDLIB_SHA256)
        require_file(stage / "python312._pth", 80, SOURCE_PTH_SHA256)
        site = stage / "Lib/site-packages"
        site.mkdir(parents=True)
        extract_zip(pip_wheel, site)
        (stage / "python312._pth").write_bytes(RUNTIME_PTH)
        (site / "paper-curation-repository.pth").write_text("../../../..\n", encoding="utf-8")
        (site / "sitecustomize.py").write_text(
            "import site,sys\n"
            "site.ENABLE_USER_SITE=False\n"
            "user_site=site.getusersitepackages()\n"
            "if user_site in sys.path: sys.path.remove(user_site)\n",
            encoding="utf-8",
        )
        environment = {"PIP_CONFIG_FILE": os.devnull, "PYTHONUTF8": "1", "SYSTEMROOT": os.environ["SYSTEMROOT"]}
        result = subprocess.run(
            [str(stage / "python.exe"), "-I", "-m", "pip", "--isolated", "install", "--require-hashes", "--no-deps", "-r", str(requirements)],
            cwd=stage,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ProvisionError(f"locked pip install failed: {result.stderr[-1000:]}")
        version = subprocess.run(
            [str(stage / "python.exe"), "-I", "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
            cwd=stage,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if version != "3.12.10":
            raise ProvisionError(f"unexpected runtime version: {version}")
        payload = (json.dumps(attest(stage, requirements), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        stage.rename(target)
        write_create_new(json_out, payload)
        runtime_attestation = target.parent.parent / ".omo/runtime/python312-resolved.json"
        if runtime_attestation.parent.is_dir() and runtime_attestation.resolve() != json_out.resolve():
            write_create_new(runtime_attestation, payload)
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        raise ProvisionError(str(error)) from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _emit_runtime(runtime: object) -> None:
    """Emit only non-secret runtime identity details for operators and tests."""
    executable = getattr(runtime, "executable")
    version = getattr(runtime, "version")
    trust_kind = getattr(runtime, "trust_kind")
    print(json.dumps({"executable": str(executable), "python_version": version, "status": "pass", "trust_kind": trust_kind}, sort_keys=True))


def main() -> int:
    """Parse check, qualification, and legacy frozen-recovery commands."""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--stage-candidate", type=Path)
    mode.add_argument("--promote-staged", action="store_true")
    mode.add_argument("--rollback-previous", action="store_true")
    mode.add_argument("--qualify-candidate", type=Path)
    mode.add_argument("--list-candidates", action="store_true")
    parser.add_argument("--project-root", type=Path, default=REPO_ROOT)
    # Legacy frozen-archive recovery arguments.  They are kept optional here
    # so maintenance modes do not need an archive or a wheel.
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--pip-wheel", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    try:
        root = args.project_root.resolve()
        if args.check_only:
            _emit_runtime(validate_runtime(root, verify_files=True))
            return 0
        if args.list_candidates:
            print(json.dumps({"candidates": [str(path) for path in discover_candidates(root)]}, sort_keys=True))
            return 0
        if args.qualify_candidate is not None:
            _emit_runtime(qualify_candidate(args.qualify_candidate, root))
            return 0
        if args.stage_candidate is not None:
            _emit_runtime(stage_candidate(args.stage_candidate, root))
            return 0
        if args.promote_staged:
            _emit_runtime(promote_staged_candidate(root))
            return 0
        if args.rollback_previous:
            _emit_runtime(rollback_previous_runtime(root))
            return 0
        legacy = (args.archive, args.target, args.pip_wheel, args.requirements, args.json_out)
        if any(value is None for value in legacy):
            parser.error("choose a maintenance mode or provide all legacy archive arguments")
        assert args.archive is not None and args.target is not None and args.pip_wheel is not None and args.requirements is not None and args.json_out is not None
        provision(args.archive.resolve(), args.target.resolve(), args.pip_wheel.resolve(), args.requirements.resolve(), args.json_out.resolve())
    except (ProvisionError, PythonRuntimeError) as error:
        print(f"runtime bootstrap denied: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
