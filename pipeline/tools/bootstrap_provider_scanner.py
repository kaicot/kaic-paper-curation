#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
# ─── How to run ───
# 1. Use the repository-attested Python 3.12 runtime.
# 2. Run: .tools/python312/python.exe pipeline/tools/bootstrap_provider_scanner.py --node-attestation .omo/runtime/node-resolved.json --scanner-lock pipeline/provider-scanner.lock.json
# ──────────────────
"""Install and attest locked Acorn using only Task-4-attested Node/npm."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ScannerBootstrapError(RuntimeError):
    """The locked provider scanner could not be safely resolved."""

    detail: str

    def __str__(self) -> str:
        return self.detail


def digest(path: Path) -> str:
    """Return the SHA-256 of a regular file."""
    if not path.is_file() or path.is_symlink():
        raise ScannerBootstrapError(f"regular file required: {path}")
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def load_object(path: Path) -> JsonObject:
    """Read a JSON object boundary."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScannerBootstrapError(f"JSON object required: {path}")
    return value


def object_value(mapping: JsonObject, key: str) -> JsonObject:
    """Return one required nested JSON object."""
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ScannerBootstrapError(f"JSON object required at {key}")
    return value


def string_value(mapping: JsonObject, key: str) -> str:
    """Return one required JSON string."""
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ScannerBootstrapError(f"JSON string required at {key}")
    return value


def canonical(payload: JsonObject) -> bytes:
    """Encode deterministic attestation bytes."""
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def verify_lock(root: Path, lock_path: Path, lock: JsonObject) -> tuple[Path, Path, Path]:
    """Bind package files and Acorn registry identity to the checked lock."""
    package_json = root / str(lock["package_json"])
    package_lock = root / str(lock["package_lock"])
    parser = root / str(lock["parser"])
    package = load_object(package_json)
    npm_lock = load_object(package_lock)
    acorn = lock["acorn"]
    if not isinstance(acorn, dict):
        raise ScannerBootstrapError("acorn lock row must be an object")
    installed = npm_lock["packages"]
    if not isinstance(installed, dict):
        raise ScannerBootstrapError("npm packages map required")
    row = installed["node_modules/acorn"]
    if not isinstance(row, dict):
        raise ScannerBootstrapError("Acorn package row required")
    dependencies = package["dependencies"]
    if not isinstance(dependencies, dict):
        raise ScannerBootstrapError("package dependencies map required")
    expected = (acorn["version"], acorn["tarball"], acorn["integrity"])
    observed = (row["version"], row["resolved"], row["integrity"])
    if dependencies.get("acorn") != acorn["version"] or observed != expected:
        raise ScannerBootstrapError("Acorn registry lock drift")
    digest(parser)
    return package_json, package_lock, parser


def verify_node(node_attestation_path: Path, node_info: JsonObject) -> tuple[Path, Path]:
    """Rehash the exact Node/npm files attested by Todo 4."""
    node = Path(str(node_info["node_path"])).resolve()
    npm = Path(str(node_info["npm_cli_path"])).resolve()
    if digest(node) != node_info["node_sha256"] or digest(npm) != node_info["npm_cli_sha256"]:
        raise ScannerBootstrapError("Task-4 Node/npm hash drift")
    node_version = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    npm_version = subprocess.run([str(node), str(npm), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if node_version != node_info["node_version"] or npm_version != node_info["npm_version"]:
        raise ScannerBootstrapError("Task-4 Node/npm version drift")
    digest(node_attestation_path)
    return node, npm


def provision(root: Path, node: Path, npm: Path, package_json: Path, package_lock: Path) -> Path:
    """Run npm ci with scripts and ambient module resolution disabled."""
    target = root / ".tools/provider-scanner"
    if not target.exists():
        target.mkdir(parents=True)
        shutil.copyfile(package_json, target / "package.json")
        shutil.copyfile(package_lock, target / "package-lock.json")
        cache = root / ".tools/provider-scanner-cache/task-3"
        cache.mkdir(parents=True, exist_ok=True)
        environment = {
            "SYSTEMROOT": os.environ["SYSTEMROOT"],
            "PATH": str(node.parent),
            "NODE_PATH": "",
            "TEMP": os.environ.get("TEMP", str(cache)),
            "TMP": os.environ.get("TMP", str(cache)),
            "npm_config_cache": str(cache),
            "npm_config_ignore_scripts": "true",
        }
        result = subprocess.run(
            [str(node), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=target,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ScannerBootstrapError(f"npm ci failed: {result.stderr[-1000:]}")
    if (target / "package.json").read_bytes() != package_json.read_bytes():
        raise ScannerBootstrapError("installed package.json drift")
    if (target / "package-lock.json").read_bytes() != package_lock.read_bytes():
        raise ScannerBootstrapError("installed package-lock.json drift")
    return target


def publish(path: Path, payload: JsonObject) -> None:
    """Publish or verify an exact canonical scanner attestation."""
    encoded = canonical(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ScannerBootstrapError(f"scanner attestation drift: {path}")
        return
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    """Resolve the locked scanner and record its canonical identities."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--node-attestation", type=Path, required=True)
    parser.add_argument("--scanner-lock", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        node_attestation = args.node_attestation.resolve()
        scanner_lock = args.scanner_lock.resolve()
        lock = load_object(scanner_lock)
        package_json, package_lock, parser_path = verify_lock(root, scanner_lock, lock)
        node_info = load_object(node_attestation)
        node, npm = verify_node(node_attestation, node_info)
        target = provision(root, node, npm, package_json, package_lock)
        acorn_lock = object_value(lock, "acorn")
        acorn = target / string_value(acorn_lock, "entrypoint")
        target_resolved = target.resolve()
        if target_resolved not in acorn.resolve().parents:
            raise ScannerBootstrapError("Acorn escaped the repository-local scanner prefix")
        payload: JsonObject = {
            "schema_version": 1,
            "acorn_file_url": acorn.resolve().as_uri(),
            "acorn_path": str(acorn.resolve()),
            "acorn_sha256": digest(acorn),
            "acorn_version": string_value(acorn_lock, "version"),
            "node_attestation_sha256": digest(node_attestation),
            "node_path": str(node),
            "node_sha256": digest(node),
            "node_version": str(node_info["node_version"]),
            "npm_cli_path": str(npm),
            "npm_cli_sha256": digest(npm),
            "npm_version": str(node_info["npm_version"]),
            "package_json_sha256": digest(package_json),
            "package_lock_sha256": digest(package_lock),
            "parser_path": str(parser_path.resolve()),
            "parser_sha256": digest(parser_path),
            "scanner_lock_sha256": digest(scanner_lock),
        }
        publish(root / ".omo/runtime/provider-scanner-resolved.json", payload)
        sys.stdout.buffer.write(canonical(payload))
    except (OSError, KeyError, json.JSONDecodeError, ScannerBootstrapError, subprocess.SubprocessError) as error:
        print(f"Provider scanner denied: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
