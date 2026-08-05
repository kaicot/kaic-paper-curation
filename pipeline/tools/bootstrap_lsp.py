#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
"""Provision or verify the frozen portable Node and LSP toolchain."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class ToolchainError(RuntimeError):
    """A locked Node/LSP input or installed artifact drifted."""


def digest(path: Path) -> str:
    """Return a streaming SHA-256 digest."""
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def load_json(path: Path) -> JsonObject:
    """Load one checked-in JSON lock."""
    if not path.is_file() or path.is_symlink():
        raise ToolchainError(f"regular lock required: {path}")
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ToolchainError(f"JSON object required: {path}")
    return value


def canonical(value: JsonObject) -> bytes:
    """Encode canonical UTF-8 JSON."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def publish(path: Path, value: JsonObject, verify_only: bool) -> None:
    """Create an attestation or require exact existing bytes."""
    payload = canonical(value)
    if verify_only:
        if not path.is_file() or path.read_bytes() != payload:
            raise ToolchainError(f"attestation drift: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def verify_node(node_dir: Path, lock: JsonObject) -> JsonObject:
    """Verify exact Node/npm binary identities and versions."""
    node = node_dir / "node.exe"
    npm = node_dir / "node_modules/npm/bin/npm-cli.js"
    if digest(node) != lock["node_executable_sha256"] or digest(npm) != lock["npm_cli_sha256"]:
        raise ToolchainError("Node or npm file hash drift")
    node_version = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    npm_version = subprocess.run([str(node), str(npm), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if node_version != lock["version"] or npm_version != lock["npm_version"]:
        raise ToolchainError("Node or npm version drift")
    return {
        "schema_version": 1,
        "archive_sha256": lock["archive_sha256"],
        "node_path": str(node.resolve()),
        "node_sha256": digest(node),
        "node_version": node_version,
        "npm_cli_path": str(npm.resolve()),
        "npm_cli_sha256": digest(npm),
        "npm_version": npm_version,
    }


def provision_node(root: Path, lock: JsonObject) -> None:
    """Download and atomically publish the frozen portable Node archive."""
    node_dir = root / ".tools/node"
    if node_dir.exists():
        return
    runtime = root / ".omo/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    archive = runtime / Path(lock["archive_url"]).name
    if not archive.exists():
        with urllib.request.urlopen(lock["archive_url"], timeout=60) as response, archive.open("xb") as stream:
            shutil.copyfileobj(response, stream)
    if archive.stat().st_size != lock["archive_size"] or digest(archive) != lock["archive_sha256"]:
        raise ToolchainError("Node archive drift")
    stage = Path(tempfile.mkdtemp(prefix=".node-", dir=root / ".tools"))
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(stage)
        extracted = stage / f"node-{lock['version']}-win-x64"
        extracted.rename(node_dir)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_lock_contract(lock: JsonObject, package_lock: JsonObject) -> None:
    """Bind top-level packages to exact registry tarballs and integrities."""
    resolved = package_lock["packages"]
    for package in lock["packages"]:
        row = resolved[f"node_modules/{package['name']}"]
        if row["version"] != package["version"] or row["resolved"] != package["tarball"] or row["integrity"] != package["integrity"]:
            raise ToolchainError(f"npm registry lock drift: {package['name']}")


def provision_lsp(root: Path, node_info: JsonObject, verify_only: bool) -> JsonObject:
    """Install with npm ci and attest the exact executable modules."""
    source = root / "pipeline/dev-tools"
    package_lock = load_json(source / "lsp-package-lock.json")
    lock = load_json(source / "lsp-lock.json")
    verify_lock_contract(lock, package_lock)
    target = root / ".tools/lsp"
    if not verify_only:
        if target.exists():
            raise ToolchainError("LSP target already exists; use --verify-only")
        target.mkdir(parents=True)
        shutil.copyfile(source / "lsp-package.json", target / "package.json")
        shutil.copyfile(source / "lsp-package-lock.json", target / "package-lock.json")
        npm = Path(node_info["npm_cli_path"])
        node = Path(node_info["node_path"])
        environment = {
            "SYSTEMROOT": os.environ["SYSTEMROOT"],
            "PATH": str(node.parent),
            "NODE_PATH": "",
            "npm_config_cache": str(root / ".tools/npm-cache"),
            "npm_config_ignore_scripts": "true",
        }
        result = subprocess.run(
            [str(node), str(npm), "ci", "--ignore-scripts", "--omit=optional", "--no-audit", "--no-fund"],
            cwd=target,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ToolchainError(f"npm ci failed: {result.stderr[-1000:]}")
    basedpyright = target / "node_modules/basedpyright/index.js"
    biome = target / "node_modules/@biomejs/cli-win32-x64/biome.exe"
    for executable in (basedpyright, biome):
        if not executable.is_file() or executable.is_symlink():
            raise ToolchainError(f"locked LSP executable missing: {executable}")
    return {
        "schema_version": 1,
        "lock_sha256": digest(source / "lsp-lock.json"),
        "package_lock_sha256": digest(source / "lsp-package-lock.json"),
        "basedpyright_path": str(basedpyright.resolve()),
        "basedpyright_sha256": digest(basedpyright),
        "biome_path": str(biome.resolve()),
        "biome_sha256": digest(biome),
    }


def main() -> int:
    """Parse the mutually exclusive provision/verify CLI."""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--provision", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--node-lock", type=Path, required=True)
    parser.add_argument("--lsp-lock", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        node_lock = load_json(args.node_lock.resolve())
        if digest(args.lsp_lock.resolve()) != digest(root / "pipeline/dev-tools/lsp-lock.json"):
            raise ToolchainError("LSP lock argument drift")
        if args.provision:
            (root / ".tools").mkdir(exist_ok=True)
            provision_node(root, node_lock)
        node_info = verify_node(root / ".tools/node", node_lock)
        lsp_info = provision_lsp(root, node_info, args.verify_only)
        publish(root / ".omo/runtime/node-resolved.json", node_info, args.verify_only)
        publish(root / ".omo/runtime/lsp-resolved.json", lsp_info, args.verify_only)
    except (OSError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, ToolchainError, zipfile.BadZipFile) as error:
        print(f"LSP bootstrap denied: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
