#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
"""Provision the frozen repository-local CPython runtime."""
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


def main() -> int:
    """Parse the bootstrap CLI boundary."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--pip-wheel", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    try:
        provision(args.archive.resolve(), args.target.resolve(), args.pip_wheel.resolve(), args.requirements.resolve(), args.json_out.resolve())
    except ProvisionError as error:
        print(f"runtime bootstrap denied: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
