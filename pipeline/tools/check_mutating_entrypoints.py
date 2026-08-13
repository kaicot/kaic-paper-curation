"""Fail-closed inventory and inert dry-run probe for mutating CLIs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import cast


SCHEMA = "mutating-entrypoints-v1"
EXPECTED_COUNTERS = {
    "auth": 0,
    "children": 0,
    "credentials": 0,
    "deploy": 0,
    "egress": 0,
    "git": 0,
    "hashes": 0,
    "writes": 0,
}
_NAME_PREFIXES = ("build_", "extract_", "generate_", "run_")
_MUTATING_NAMES = {
    "copy",
    "copy2",
    "copyfile",
    "makedirs",
    "mkdir",
    "move",
    "remove",
    "rename",
    "rmdir",
    "rmtree",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}
_EXPLICIT_NAMES = {
    "auto_recover.py",
    "classify_papers.py",
    "cleanup.py",
    "cleanup_quarantine.py",
    "dedup_text.py",
    "dedup_zotero.py",
    "prepare_local_models.py",
    "reextract_figures.py",
    "setup.py",
}


class InventoryError(RuntimeError):
    pass


def _duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InventoryError("manifest-duplicate-key")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise InventoryError(f"manifest-nonfinite:{value}")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _load_manifest(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise InventoryError("manifest-regular-file-required")
    try:
        value = cast(
            object,
            json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=_reject_constant,
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError("manifest-json-invalid") from error
    if not isinstance(value, dict):
        raise InventoryError("manifest-object-required")
    return cast(dict[str, object], value)


def _git(
    workspace: Path,
    *args: str,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise InventoryError("baseline-git-read-failed")
    return result.stdout.strip()


def _has_main(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and "__name__" in ast.unparse(node.test)
            and "__main__" in ast.unparse(node.test)
        ):
            return True
    return False


def _open_is_write(node: ast.Call) -> bool:
    name = ast.unparse(node.func).split(".")[-1]
    if name != "open":
        return False
    mode: str | None = None
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value if isinstance(node.args[1].value, str) else None
    for keyword in node.keywords:
        if (
            keyword.arg == "mode"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            mode = keyword.value.value
    return mode is not None and any(flag in mode for flag in "wax+")


def _source_findings(
    source: str,
    *,
    filename: str,
) -> list[dict[str, object]]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        raise InventoryError(f"source-ast-invalid:{filename}") from error
    findings: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func)
        leaf = callee.split(".")[-1]
        if leaf == "ArgumentParser":
            findings.append(
                {
                    "callee": callee,
                    "kind": "cli-parser",
                    "line": node.lineno,
                }
            )
        if leaf in _MUTATING_NAMES or _open_is_write(node):
            findings.append(
                {"callee": callee, "kind": "write", "line": node.lineno}
            )
        elif callee == "os.replace" or (
            leaf == "replace"
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and ast.unparse(node.func.value.func).split(".")[-1] == "Path"
        ):
            findings.append(
                {"callee": callee, "kind": "write", "line": node.lineno}
            )
        elif callee.startswith(("subprocess.", "shutil.")):
            findings.append(
                {"callee": callee, "kind": "process", "line": node.lineno}
            )
        if callee in {
            "os.execv",
            "os.execve",
            "os.system",
            "requests.delete",
            "requests.get",
            "requests.patch",
            "requests.post",
            "requests.put",
            "socket.create_connection",
            "urllib.request.urlopen",
        }:
            findings.append(
                {"callee": callee, "kind": "egress", "line": node.lineno}
            )
    return sorted(
        findings,
        key=lambda row: (
            cast(int, row["line"]),
            cast(str, row["kind"]),
            cast(str, row["callee"]),
        ),
    )


def _source_is_mutator(
    path: str,
    source: str,
    findings: list[dict[str, object]],
) -> bool:
    name = Path(path).name
    if name.startswith(_NAME_PREFIXES) or name in _EXPLICIT_NAMES:
        return True
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        raise InventoryError(f"source-ast-invalid:{path}") from error
    return _has_main(tree) and any(
        row["kind"] != "cli-parser" for row in findings
    )


def _current_inventory(path: Path, workspace: Path) -> dict[str, object] | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InventoryError(f"current-source-invalid:{path.name}") from error
    relative = path.relative_to(workspace).as_posix()
    findings = _source_findings(source, filename=relative)
    if not _source_is_mutator(relative, source, findings):
        return None
    return {"ast_findings": findings, "path": relative}


def _baseline_inventory(
    workspace: Path,
    commit: str,
) -> dict[str, dict[str, object]]:
    listing = _git(workspace, "ls-tree", "-r", commit, "--", "pipeline")
    blobs: dict[str, str] = {}
    for line in listing.splitlines():
        metadata, separator, path = line.partition("\t")
        parts = metadata.split()
        if (
            separator != "\t"
            or len(parts) != 3
            or parts[1] != "blob"
            or not path.startswith("pipeline/")
            or "/" in path.removeprefix("pipeline/")
            or not path.endswith(".py")
        ):
            continue
        blobs[path] = parts[2]
    archive = subprocess.run(
        ["git", "-C", str(workspace), "archive", "--format=tar", commit, "pipeline"],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise InventoryError("baseline-archive-read-failed")
    inventory: dict[str, dict[str, object]] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            for member in bundle.getmembers():
                path = member.name
                if path not in blobs or not member.isfile():
                    continue
                handle = bundle.extractfile(member)
                if handle is None:
                    raise InventoryError("baseline-archive-member-invalid")
                source = handle.read().decode("utf-8")
                findings = _source_findings(source, filename=path)
                if _source_is_mutator(path, source, findings):
                    inventory[path] = {
                        "ast_findings": findings,
                        "baseline_blob_sha": blobs[path],
                    }
    except (tarfile.TarError, UnicodeError, OSError) as error:
        raise InventoryError("baseline-archive-invalid") from error
    return inventory


def _validate_manifest(
    value: dict[str, object],
    workspace: Path,
) -> list[dict[str, object]]:
    if (
        value.get("schema") != SCHEMA
        or value.get("schema_version") != 1
        or not isinstance(value.get("baseline"), dict)
        or not isinstance(value.get("entrypoints"), list)
    ):
        raise InventoryError("manifest-schema-invalid")
    baseline = cast(dict[str, object], value["baseline"])
    commit = baseline.get("commit")
    count = baseline.get("entrypoint_count")
    if (
        not isinstance(commit, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
    ):
        raise InventoryError("manifest-baseline-invalid")
    expected_baseline = _baseline_inventory(workspace, commit)
    if count != len(expected_baseline):
        raise InventoryError("manifest-baseline-count-invalid")
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    baseline_rows = 0
    shape: list[dict[str, object]] = []
    for raw in cast(list[object], value["entrypoints"]):
        if not isinstance(raw, dict):
            raise InventoryError("manifest-row-invalid")
        row = cast(dict[str, object], raw)
        path_value = row.get("path")
        origin = row.get("origin")
        blob = row.get("baseline_blob_sha")
        findings = row.get("ast_findings")
        argv = row.get("dry_run_argv")
        expected_disposition = (
            "common-dry-run"
            if path_value == "pipeline/run_full.py"
            else "default-denied"
        )
        if (
            not isinstance(path_value, str)
            or not path_value.startswith("pipeline/")
            or Path(path_value).is_absolute()
            or ".." in Path(path_value).parts
            or path_value in seen
            or origin not in {"baseline", "introduced"}
            or row.get("disposition") != expected_disposition
            or not isinstance(findings, list)
            or not isinstance(argv, list)
            or not argv
            or any(
                not isinstance(item, str)
                for item in cast(list[object], argv)
            )
        ):
            raise InventoryError("manifest-row-invalid")
        seen.add(path_value)
        target = workspace / path_value
        if target.is_symlink() or not target.is_file():
            raise InventoryError(f"manifest-target-missing:{path_value}")
        if origin == "baseline":
            baseline_rows += 1
            expected = expected_baseline.get(path_value)
            if (
                not isinstance(blob, str)
                or expected is None
                or blob != expected["baseline_blob_sha"]
                or findings != expected["ast_findings"]
            ):
                raise InventoryError("manifest-baseline-blob-invalid")
        else:
            current = _current_inventory(target, workspace)
            if (
                blob is not None
                or path_value in expected_baseline
                or current is None
                or findings != current["ast_findings"]
            ):
                raise InventoryError("manifest-introduced-row-invalid")
        shape.append(
            {
                "ast_findings": findings,
                "baseline_blob_sha": blob,
                "disposition": row["disposition"],
                "origin": origin,
                "path": path_value,
            }
        )
        rows.append(row)
    if baseline_rows != count:
        raise InventoryError("manifest-baseline-count-invalid")
    baseline_seen = {
        cast(str, row["path"])
        for row in rows
        if row["origin"] == "baseline"
    }
    if baseline_seen != set(expected_baseline):
        raise InventoryError("manifest-baseline-shape-invalid")
    if [cast(str, row["path"]) for row in rows] != sorted(seen):
        raise InventoryError("manifest-order-invalid")
    expected_shape = hashlib.sha256(
        _canonical(shape).encode("utf-8")
    ).hexdigest()
    if value.get("computed_shape_sha256") != expected_shape:
        raise InventoryError("manifest-shape-hash-invalid")
    return rows


def _current_unclassified(
    workspace: Path,
    classified: set[str],
) -> list[str]:
    pipeline = workspace / "pipeline"
    result: list[str] = []
    for path in sorted(pipeline.glob("*.py")):
        relative = path.relative_to(workspace).as_posix()
        if relative in classified:
            continue
        if path.is_symlink():
            raise InventoryError(f"current-source-symlink:{relative}")
        if _current_inventory(path, workspace) is not None:
            result.append(relative)
    return result


_SITECUSTOMIZE = r'''
import builtins
import json
import os
import socket
import subprocess
from pathlib import Path

_allowed = {
    str(Path(item).resolve())
    for item in json.loads(os.environ.get("PAPER_CURATION_DRY_ALLOWED_READS", "[]"))
}
_open = builtins.open
_path_open = Path.open
_path_read_text = Path.read_text
_path_read_bytes = Path.read_bytes

def _check_read(value):
    path = str(Path(value).resolve())
    if path not in _allowed:
        raise RuntimeError("dry-run-forbidden:read")

def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"):
        raise RuntimeError("dry-run-forbidden:write")
    _check_read(file)
    return _open(file, mode, *args, **kwargs)

def guarded_path_open(self, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"):
        raise RuntimeError("dry-run-forbidden:write")
    _check_read(self)
    return _path_open(self, mode, *args, **kwargs)

def guarded_read_text(self, *args, **kwargs):
    _check_read(self)
    return _path_read_text(self, *args, **kwargs)

def guarded_read_bytes(self, *args, **kwargs):
    _check_read(self)
    return _path_read_bytes(self)

def denied(kind):
    def fail(*args, **kwargs):
        raise RuntimeError("dry-run-forbidden:" + kind)
    return fail

builtins.open = guarded_open
Path.open = guarded_path_open
Path.read_text = guarded_read_text
Path.read_bytes = guarded_read_bytes
for name in ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rename", "replace", "rmdir"):
    setattr(Path, name, denied("write"))
for name in ("run", "call", "check_call", "check_output", "Popen"):
    setattr(subprocess, name, denied("children"))
socket.create_connection = denied("egress")
socket.socket.connect = denied("egress")

class GuardedEnviron(dict):
    def _blocked(self, key):
        text = str(key).upper()
        fragments = (
            "API" + "_KEY",
            "ACCESS" + "_TOKEN",
            "CLIENT" + "_SECRET",
            "BASE" + "_URL",
        )
        return any(fragment in text for fragment in fragments)
    def __getitem__(self, key):
        if self._blocked(key):
            raise RuntimeError("dry-run-forbidden:credentials")
        return super().__getitem__(key)
    def get(self, key, default=None):
        if self._blocked(key):
            raise RuntimeError("dry-run-forbidden:credentials")
        return super().get(key, default)
    def __contains__(self, key):
        if self._blocked(key):
            raise RuntimeError("dry-run-forbidden:credentials")
        return super().__contains__(key)

os.environ = GuardedEnviron(os.environ)
'''


def probe_rows(
    rows: list[dict[str, object]],
    workspace: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="paper-curation-dry-run-") as raw:
        sentry = Path(raw)
        _ = (sentry / "sitecustomize.py").write_text(
            _SITECUSTOMIZE,
            encoding="utf-8",
        )
        for row in rows:
            raw_argv = cast(list[str], row["dry_run_argv"])
            argv = [
                sys.executable if item == "{python}" else item
                for item in raw_argv
            ]
            target = (workspace / argv[1]).resolve() if len(argv) > 1 else None
            allowed = [str(target)] if target is not None else []
            bootstrap = (
                "import runpy,sys;"
                f"exec({repr(_SITECUSTOMIZE)});"
                "sys.argv=sys.argv[1:];"
                "runpy.run_path(sys.argv[0],run_name='__main__')"
            )
            probe_argv = [
                argv[0],
                "-c",
                bootstrap,
                *argv[1:],
            ]
            environment = {
                "HOME": str(sentry),
                "HOMEDRIVE": sentry.drive,
                "HOMEPATH": str(sentry)[2:] if sentry.drive else str(sentry),
                "PATH": "",
                "PAPER_CURATION_DRY_ALLOWED_READS": json.dumps(allowed),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": str(sentry),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "TEMP": str(sentry),
                "TMP": str(sentry),
                "USERPROFILE": str(sentry),
            }
            result = subprocess.run(
                probe_argv,
                cwd=workspace,
                env=environment,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            if result.returncode != 0:
                category = "forbidden" if "dry-run-forbidden:" in result.stderr else "failed"
                raise InventoryError(
                    f"dry-run-{category}:{cast(str, row['path'])}"
                )
            try:
                payload = cast(object, json.loads(result.stdout))
            except json.JSONDecodeError as error:
                raise InventoryError(
                    f"dry-run-json-invalid:{cast(str, row['path'])}"
                ) from error
            if (
                not isinstance(payload, dict)
                or cast(dict[str, object], payload).get("schema")
                != "dry-run-plan-v1"
                or cast(dict[str, object], payload).get(
                    "forbidden_counters"
                )
                != EXPECTED_COUNTERS
                or result.stderr
            ):
                raise InventoryError(
                    f"dry-run-contract-invalid:{cast(str, row['path'])}"
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--manifest", required=True)
    _ = parser.add_argument("--workspace")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = Path(cast(str, args.manifest)).resolve()
    workspace_arg = cast(str | None, args.workspace)
    workspace = (
        Path(workspace_arg).resolve()
        if workspace_arg
        else manifest_path.parent.parent.resolve()
    )
    try:
        value = _load_manifest(manifest_path)
        rows = _validate_manifest(value, workspace)
        unclassified = _current_unclassified(
            workspace,
            {cast(str, row["path"]) for row in rows},
        )
        if unclassified:
            raise InventoryError(
                "unclassified-mutator:" + ",".join(unclassified)
            )
        probe_rows(rows, workspace)
    except (InventoryError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        _canonical(
            {
                "entrypoints": len(rows),
                "forbidden_counters": EXPECTED_COUNTERS,
                "result": "PASS",
                "schema": "mutating-entrypoints-check-v1",
                "schema_version": 1,
                "unclassified": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
