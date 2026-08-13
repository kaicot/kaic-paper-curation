#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
"""Run deterministic diagnostics through the attested local toolchain."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class DiagnosticError(RuntimeError):
    """The diagnostic selector, mapping, or terminal engine failed."""


def digest(path: Path) -> str:
    """Return one file's SHA-256 digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_object(path: Path) -> JsonObject:
    """Load a JSON object from a regular file."""
    if not path.is_file() or path.is_symlink():
        raise DiagnosticError(f"regular JSON file required: {path}")
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise DiagnosticError(f"JSON object required: {path}")
    return value


def write_new(path: Path, payload: bytes) -> None:
    """Publish one create-new diagnostic artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def changed_files(root: Path, git: Path, changed_from: str, includes: list[str]) -> list[Path]:
    """Resolve the changed-range selector with exact no-shell Git argv."""
    if not git.is_absolute() or not git.is_file():
        raise DiagnosticError("--git must be an absolute executable file")
    result = subprocess.run(
        [str(git), "diff", "--name-only", "-z", changed_from, "--"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise DiagnosticError(f"git changed-range failed: {result.stderr.decode('utf-8', 'replace')}")
    names = [name.decode("utf-8") for name in result.stdout.split(b"\0") if name]
    selected = [root / name for name in names if any(fnmatch.fnmatchcase(name, pattern) for pattern in includes)]
    return sorted(selected, key=lambda path: path.as_posix().encode("utf-8"))


def run_file(root: Path, path: Path, engine: str, node: Path, lsp: JsonObject) -> dict[str, int | str]:
    """Run exactly one terminal engine for one declared file mapping."""
    if not path.is_file() or path.is_symlink():
        raise DiagnosticError(f"diagnostic input must be a regular file: {path}")
    executables = {
        "basedpyright": Path(lsp["basedpyright_path"]),
        "biome": Path(lsp["biome_path"]),
    }
    executable = executables.get(engine)
    if executable is None:
        raise DiagnosticError(f"unmapped diagnostic engine: {engine}")
    arguments = {
        "basedpyright": [str(node), str(executable), "--outputjson", str(path.resolve())],
        "biome": [str(executable), "check", "--reporter=json", str(path.resolve())],
    }
    argv = arguments[engine]
    if not executable.is_file() or executable.is_symlink():
        raise DiagnosticError(f"attested engine missing: {executable}")
    expected_hash = lsp[f"{engine}_sha256"]
    if digest(executable) != expected_hash:
        raise DiagnosticError(f"attested engine hash drift: {engine}")
    environment = {"SYSTEMROOT": os.environ["SYSTEMROOT"], "PATH": str(node.parent), "NODE_PATH": ""}
    result = subprocess.run(argv, cwd=root, env=environment, capture_output=True, text=True, check=False)
    return {
        "engine": engine,
        "exit_code": result.returncode,
        "file": path.resolve().relative_to(root).as_posix(),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
    }


def main() -> int:
    """Parse explicit-file or changed-range diagnostic selection."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--files", type=Path, nargs="+")
    selector.add_argument("--changed-from")
    parser.add_argument("--git", type=Path)
    parser.add_argument("--include", action="append")
    parser.add_argument("--changed-files-out", type=Path)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        bridge_path = args.bridge.resolve()
        lock_path = args.lock.resolve() if args.lock else root / "pipeline/dev-tools/lsp-lock.json"
        bridge = load_object(bridge_path)
        load_object(lock_path)
        node = root / ".tools/node/node.exe"
        lsp = load_object(root / ".omo/runtime/lsp-resolved.json")
        node_info = load_object(root / ".omo/runtime/node-resolved.json")
        if digest(node) != node_info["node_sha256"]:
            raise DiagnosticError("attested Node hash drift")
        if args.files:
            if args.git or args.include or args.changed_files_out:
                raise DiagnosticError("changed-range arguments are forbidden with --files")
            files = [path.resolve() for path in args.files]
        else:
            if not args.git or not args.include or not args.changed_files_out:
                raise DiagnosticError("changed-range mode requires --git, --include, and --changed-files-out")
            files = changed_files(root, args.git.resolve(), args.changed_from, args.include)
            write_new(args.changed_files_out.resolve(), ("".join(f"{path.relative_to(root).as_posix()}\n" for path in files)).encode())
        mappings = bridge["extensions"]
        results = []
        for path in files:
            engine = mappings.get(path.suffix.lower())
            if not isinstance(engine, str):
                raise DiagnosticError(f"no engine mapping for: {path.suffix}")
            results.append(run_file(root, path, engine, node, lsp))
        if len(results) != len(files) or any(result["exit_code"] != 0 for result in results):
            raise DiagnosticError("one or more terminal diagnostics failed")
        payload = {"bridge_sha256": digest(bridge_path), "lock_sha256": digest(lock_path), "results": results, "schema_version": 1}
        write_new(args.json_out.resolve(), (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
    except (OSError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, DiagnosticError) as error:
        print(f"LSP diagnostics denied: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
