#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
# ─── How to run ───
# 1. Use the repository-attested Python 3.12 runtime.
# 2. Run: .tools/python312/python.exe pipeline/tools/check_provider_inventory.py --patterns pipeline/provider-scan-patterns-v1.json --manifest pipeline/provider-entrypoints.json --scanner-lock pipeline/provider-scanner.lock.json --baseline <sha> --allow-owned-baseline-violations --owners 9-15
# ──────────────────
"""Compare immutable-baseline provider shapes with the current worktree."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import cast


ROOT = Path(__file__).resolve().parents[2]
QUARANTINE_PATH = "pipeline/providers/paid_compat.py"
CLEAN_DEFAULT_MODULES = (
    "pipeline.doctor",
    "pipeline.provider_inventory",
    "pipeline.providers.codex_gateway",
    "pipeline.runtime_policy",
    "pipeline.tools.check_provider_inventory",
    "pipeline.tools.resolve_runtime_policy",
)
sys.path.insert(0, str(ROOT))

from pipeline.provider_inventory import (  # noqa: E402
    InventoryError,
    JsonObject,
    JsonValue,
    canonical,
    digest,
    digest_bytes,
    load_object,
    normalize,
    object_value,
    scan_source,
    source_paths,
    string_list,
    string_value,
)


GIT = Path(r"C:\Program Files\Git\cmd\git.exe")


def run_git(root: Path, *argv: str, binary: bool = False) -> bytes:
    """Run the Task-1-attested absolute Git without a shell."""
    result = subprocess.run([str(GIT), *argv], cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        raise InventoryError(f"git {' '.join(argv)} failed: {result.stderr.decode(errors='replace')[-500:]}")
    return result.stdout if binary else result.stdout.replace(b"\r\n", b"\n")


def verify_scanner(lock_path: Path, attestation_path: Path) -> JsonObject:
    """Verify all tool identities before parsing any JavaScript."""
    scanner = load_object(attestation_path)
    checks = {
        "scanner_lock_sha256": digest(lock_path),
        "acorn_sha256": digest(Path(string_value(scanner, "acorn_path"))),
        "node_sha256": digest(Path(string_value(scanner, "node_path"))),
        "npm_cli_sha256": digest(Path(string_value(scanner, "npm_cli_path"))),
        "parser_sha256": digest(Path(string_value(scanner, "parser_path"))),
    }
    for key, observed in checks.items():
        if scanner.get(key) != observed:
            raise InventoryError(f"scanner tool/hash drift: {key}")
    acorn = Path(string_value(scanner, "acorn_path")).resolve()
    provider_scanner = acorn.parents[3]
    if (
        provider_scanner.name != "provider-scanner"
        or provider_scanner.parent.name != ".tools"
        or not acorn.is_file()
        or acorn.is_symlink()
        or string_value(scanner, "acorn_file_url") != acorn.as_uri()
    ):
        raise InventoryError("Acorn path/file URL escaped the locked prefix")
    return scanner


def scan_baseline(root: Path, baseline: str, patterns: JsonObject, scanner: JsonObject) -> list[JsonObject]:
    """Scan immutable Git blobs and bind each row to its blob SHA."""
    names = run_git(root, "ls-tree", "-r", "--name-only", baseline, "--", *string_list(patterns, "roots"))
    extensions = set(string_list(patterns, "extensions"))
    excluded = {normalize(value) for value in string_list(patterns, "excluded_paths")}
    rows: list[JsonObject] = []
    for raw in names.decode("utf-8").splitlines():
        path = normalize(raw)
        if Path(path).suffix.lower() not in extensions or path in excluded:
            continue
        source = run_git(root, "show", f"{baseline}:{path}", binary=True)
        reasons = scan_source(path, source, patterns, scanner)
        if reasons:
            blob = run_git(root, "rev-parse", f"{baseline}:{path}").decode().strip()
            reason_values: list[JsonValue] = [reason for reason in reasons]
            row: JsonObject = {"baseline_blob_sha": blob, "discovery_reasons": reason_values, "path": path}
            rows.append(row)
    return rows


def scan_worktree(root: Path, patterns: JsonObject, scanner: JsonObject) -> list[JsonObject]:
    """Scan current files independently of Git baseline blobs."""
    rows: list[JsonObject] = []
    for path in source_paths(root, patterns):
        reasons = scan_source(path, (root / path).read_bytes(), patterns, scanner)
        if reasons:
            reason_values: list[JsonValue] = [reason for reason in reasons]
            row: JsonObject = {
                "discovery_reasons": reason_values,
                "path": path,
            }
            rows.append(row)
    return rows


def owner(path: str) -> int:
    """Assign each frozen baseline path to its unique cleanup todo."""
    if path == "pipeline/run_update_force.py":
        return 9
    if path == "pipeline/topic_modeling.py":
        return 10
    if path == "pipeline/build_category_summaries.py":
        return 11
    if path == "pipeline/extract_insights.py":
        return 12
    if path == "pipeline/generate_timelines.py":
        return 13
    return 15


def capability(path: str) -> str:
    """Classify the default-denied paid capability represented by a path."""
    lowered = path.lower()
    if "audio" in lowered:
        return "audio-generation"
    if "deploy" in lowered or lowered.startswith("worker/"):
        return "public-provider-proxy"
    if any(token in lowered for token in ("search_index", "retrieval", "query_search")):
        return "dense-embedding"
    if any(token in lowered for token in ("paperbanana", "figure", "workflow", "schematic", "timeline_pb")):
        return "image-generation"
    return "text-generation"


def computed_shape(rows: list[JsonObject]) -> str:
    """Bind the exact baseline path/blob/reason tuples."""
    return digest_bytes(canonical(rows))


def digest_manifest_input(path: Path) -> str:
    """Hash repository text without platform-specific checkout line endings."""
    return digest_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def generated_manifest(baseline: str, patterns_path: Path, lock_path: Path, rows: list[JsonObject]) -> JsonObject:
    """Build the canonical provider-entrypoints-v1 document."""
    entries: list[JsonValue] = []
    for row in rows:
        path = string_value(row, "path")
        assigned = owner(path)
        entries.append(
            {
                **row,
                "capability": capability(path),
                "default_reachable": False,
                "disposition": "default-denied" if assigned < 15 else "quarantine",
                "owner_todo": assigned,
            }
        )
    lexical_count = len(
        {
            string_value(row, "path")
            for row in rows
            if "lexical" in string_list(row, "discovery_reasons")
        }
    )
    return {
        "schema": "provider-entrypoints-v1",
        "schema_version": 1,
        "baseline": {
            "commit": baseline,
            "entrypoint_count": len(rows),
            "lexical_file_count": lexical_count,
        },
        "computed_shape_sha256": computed_shape(rows),
        "patterns_sha256": digest_manifest_input(patterns_path),
        "scanner_lock_sha256": digest_manifest_input(lock_path),
        "entrypoints": entries,
    }


def parse_owners(value: str) -> set[int]:
    """Parse the canonical inclusive owner range."""
    start, end = value.split("-", 1)
    owners = set(range(int(start), int(end) + 1))
    if not owners or min(owners) < 9 or max(owners) > 15:
        raise InventoryError("owners must be an inclusive subset of 9-15")
    if not owners or any(owner not in range(9, 16) for owner in owners):
        raise InventoryError("owners must be an exact subset of Todo 9 through 15")
    return owners


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def finding_multiset(
    path: str,
    source: bytes,
    patterns: JsonObject,
) -> Counter[tuple[str, str, str]]:
    """Return normalized individual provider findings with their exact counts."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InventoryError(f"provider source is not UTF-8: {path}") from error
    findings: list[tuple[str, str, str]] = []
    for pattern in string_list(patterns, "lexical"):
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            findings.append(("lexical", pattern, match.group(0).casefold()))
    for key in string_list(patterns, "environment_keys"):
        for match in re.finditer(re.escape(key), text):
            findings.append(("environment-key", key, match.group(0)))
    for host in string_list(patterns, "provider_hosts"):
        for match in re.finditer(re.escape(host), text, flags=re.IGNORECASE):
            findings.append(("provider-host", host, match.group(0).casefold()))
    if Path(path).suffix.lower() == ".py":
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as error:
            raise InventoryError(f"provider Python parse failed: {path}") from error
        provider_modules = set(string_list(patterns, "provider_modules"))
        constructors = set(string_list(patterns, "constructor_names"))
        environment_keys = set(string_list(patterns, "environment_keys"))
        provider_hosts = set(string_list(patterns, "provider_hosts"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in provider_modules or any(
                        alias.name.startswith(f"{module}.")
                        for module in provider_modules
                    ):
                        findings.append(("python-import", alias.name, alias.asname or ""))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in provider_modules or any(
                    module.startswith(f"{item}.")
                    for item in provider_modules
                ):
                    findings.append(("python-import-from", module, str(node.level)))
            elif isinstance(node, ast.Call):
                called = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else ""
                )
                if called in constructors:
                    findings.append(("python-constructor", called, str(len(node.args))))
                if node.args:
                    literal = _constant_string(node.args[0])
                    if literal in provider_modules:
                        findings.append(("python-dynamic-import", called, literal))
                    if literal in environment_keys:
                        findings.append(("python-environment-read", called, literal))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in environment_keys:
                    findings.append(("python-environment-literal", node.value, ""))
                for host in provider_hosts:
                    if host.casefold() in node.value.casefold():
                        findings.append(("python-provider-host", host, node.value.casefold()))
    return Counter(findings)


def finding_fingerprint(path: str, source: bytes, patterns: JsonObject) -> str:
    """Hash the normalized provider-finding multiset for stable evidence."""
    findings = finding_multiset(path, source, patterns)
    encoded = json.dumps(
        sorted((finding, count) for finding, count in findings.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest_bytes(encoded)


def reject_reverse_quarantine_imports(root: Path, patterns: JsonObject) -> None:
    """No production source may import the metadata-only quarantine."""
    for relative in source_paths(root, patterns):
        if relative in {QUARANTINE_PATH, "pipeline/tests/test_provider_boundary.py"}:
            continue
        if Path(relative).suffix.lower() != ".py":
            continue
        try:
            tree = ast.parse((root / relative).read_bytes(), filename=relative)
        except (SyntaxError, UnicodeDecodeError) as error:
            raise InventoryError(f"provider reverse-import parse failed: {relative}") from error
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "pipeline.providers.paid_compat"
                for alias in node.names
            ):
                raise InventoryError(f"production imports paid quarantine: {relative}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "pipeline.providers"
                and any(alias.name == "paid_compat" for alias in node.names)
            ):
                raise InventoryError(f"production imports paid quarantine: {relative}")


def validate_quarantine_source(source: bytes) -> None:
    """Permit provider names as metadata, never imports, constructors, or public callables."""
    tree = ast.parse(source, filename=QUARANTINE_PATH)
    provider_modules = {
        "anthropic",
        "google.genai",
        "google.generativeai",
        "openai",
    }
    constructors = {"Anthropic", "AsyncAnthropic", "OpenAI", "AsyncOpenAI", "GenerativeModel"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name in provider_modules
                or any(alias.name.startswith(f"{module}.") for module in provider_modules)
                for alias in node.names
            ):
                raise InventoryError("paid quarantine imports a provider SDK")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in provider_modules or any(module.startswith(f"{item}.") for item in provider_modules):
                raise InventoryError("paid quarantine imports a provider SDK")
        elif isinstance(node, ast.Call):
            called = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            )
            if called in constructors:
                raise InventoryError("paid quarantine constructs a provider client")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("__"):
                raise InventoryError(f"paid quarantine exposes callable: {node.name}")


def paths_for_owner(manifest: JsonObject, owner_todo: int) -> list[str]:
    """Return every uniquely assigned frozen path for one migration owner."""
    entrypoints = manifest.get("entrypoints")
    if not isinstance(entrypoints, list):
        raise InventoryError("manifest entrypoints must be a list")
    paths: list[str] = []
    seen: set[str] = set()
    for raw in entrypoints:
        if not isinstance(raw, dict):
            raise InventoryError("manifest entrypoints must be objects")
        path = string_value(raw, "path")
        if path in seen:
            raise InventoryError(f"multiply owned or duplicate path: {path}")
        seen.add(path)
        assigned = raw.get("owner_todo")
        if not isinstance(assigned, int) or isinstance(assigned, bool) or assigned not in range(9, 16):
            raise InventoryError(f"invalid unique owner: {path}")
        if assigned == owner_todo:
            paths.append(path)
    return sorted(paths)


def _poison_environment() -> dict[str, str]:
    environment = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def poison_import_modules(root: Path, modules: tuple[str, ...]) -> list[str]:
    """Import exact modules with paid SDK, egress, subprocess, and writes poisoned."""
    harness = r'''
import builtins
import importlib
import importlib.abc
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "pipeline"))
provider_roots = ("anthropic", "openai")
provider_prefixes = ("google.genai", "google.generativeai")
paid_key_prefixes = ("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_", "AZURE_")
paid_selectors = ("API_PROVIDER", "ALLOW_PAID_API", "LLM_MODE", "BASE_URL")

class ProviderPoison(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in provider_roots or fullname.startswith(tuple(name + "." for name in provider_roots)):
            raise RuntimeError("provider-import-poison:" + fullname)
        if fullname in provider_prefixes or fullname.startswith(tuple(name + "." for name in provider_prefixes)):
            raise RuntimeError("provider-import-poison:" + fullname)
        return None

def denied(*args, **kwargs):
    raise RuntimeError("provider-boundary-side-effect-poison")

class PoisonEnvironment(dict):
    def _check(self, key):
        name = str(key).upper()
        if name.startswith(paid_key_prefixes) or any(token in name for token in paid_selectors):
            raise RuntimeError("provider-environment-read-poison:" + name)
    def __getitem__(self, key):
        self._check(key)
        return super().__getitem__(key)
    def get(self, key, default=None):
        self._check(key)
        return super().get(key, default)
    def pop(self, key, *args):
        self._check(key)
        return super().pop(key, *args)
    def setdefault(self, key, default=None):
        self._check(key)
        return super().setdefault(key, default)

os.environ = PoisonEnvironment(os.environ)
real_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        denied(file, mode)
    return real_open(file, mode, *args, **kwargs)

sys.meta_path.insert(0, ProviderPoison())
builtins.open = guarded_open
pathlib.Path.write_bytes = denied
pathlib.Path.write_text = denied
pathlib.Path.touch = denied
pathlib.Path.mkdir = denied
pathlib.Path.unlink = denied
pathlib.Path.rename = denied
pathlib.Path.replace = denied
socket.create_connection = denied
socket.socket.connect = denied
subprocess.Popen = denied
subprocess.run = denied
subprocess.call = denied
subprocess.check_call = denied
subprocess.check_output = denied

imported = []
for name in sys.argv[2:]:
    if name.startswith("@"):
        relative = name[1:]
        source_path = root / relative
        synthetic = "_boundary_" + "".join(
            character if character.isalnum() else "_"
            for character in relative
        )
        spec = importlib.util.spec_from_file_location(synthetic, source_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("provider-import-spec-poison:" + relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[synthetic] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(name)
    module_path = pathlib.Path(module.__file__).resolve()
    if root not in module_path.parents:
        raise RuntimeError("provider-import-provenance-poison:" + name)
    imported.append(name[1:] if name.startswith("@") else name)
if any(
    name in provider_roots
    or name in provider_prefixes
    or name.startswith(tuple(item + "." for item in provider_roots + provider_prefixes))
    for name in sys.modules
):
    raise RuntimeError("provider-module-cache-poison")
sys.stdout.write(json.dumps(imported, separators=(",", ":")))
'''
    completed = subprocess.run(
        [sys.executable, "-c", harness, str(root), *modules],
        cwd=root,
        env=_poison_environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise InventoryError(f"provider-poison import failed: {detail}")
    value = cast(JsonValue, json.loads(completed.stdout))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InventoryError("provider-poison import returned invalid evidence")
    return [item for item in value if isinstance(item, str)]


def _module_name(path: str) -> str:
    candidate = Path(path)
    parts = list(candidate.with_suffix("").parts)
    if parts[-1] == "__init__":
        _ = parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return "@" + path
    return ".".join(parts)


def poison_import_paths(root: Path, paths: list[str], scanner: JsonObject) -> list[str]:
    """Import every owned Python/JavaScript path under the provider poison."""
    python_paths = [path for path in paths if Path(path).suffix.lower() == ".py"]
    javascript_paths = [
        path
        for path in paths
        if Path(path).suffix.lower() in {".js", ".mjs"}
    ]
    imported = poison_import_modules(root, tuple(_module_name(path) for path in python_paths))
    if javascript_paths:
        harness = r'''
const fs = require("fs");
const { pathToFileURL } = require("url");
const denied = () => { throw new Error("provider-boundary-side-effect-poison"); };
for (const name of ["writeFileSync", "appendFileSync", "renameSync", "unlinkSync", "mkdirSync"]) {
  fs[name] = denied;
}
globalThis.fetch = denied;
globalThis.addEventListener = () => {};
(async () => {
  const root = process.argv[1];
  const imported = [];
  for (const relative of process.argv.slice(2)) {
    await import(pathToFileURL(root + "/" + relative).href);
    imported.push(relative);
  }
  process.stdout.write(JSON.stringify(imported));
})().catch((error) => { console.error(String(error)); process.exit(2); });
'''
        node = Path(string_value(scanner, "node_path"))
        completed = subprocess.run(
            [str(node), "-e", harness, str(root), *javascript_paths],
            cwd=root,
            env=_poison_environment(),
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise InventoryError(f"provider-poison JavaScript import failed: {detail}")
        imported.extend(javascript_paths)
    return imported


@dataclass(frozen=True)
class Arguments:
    patterns: Path
    manifest: Path | None
    scanner_lock: Path
    scanner_attestation: Path | None
    baseline: str
    allow_owned_baseline_violations: bool
    owners: str
    require_owner_clean: int | None
    require_zero_unresolved: bool
    write_manifest: bool


def parse_arguments() -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--patterns", type=Path, required=True)
    _ = parser.add_argument("--manifest", type=Path)
    _ = parser.add_argument("--scanner-lock", type=Path, required=True)
    _ = parser.add_argument("--scanner-attestation", type=Path)
    _ = parser.add_argument("--baseline", required=True)
    _ = parser.add_argument("--allow-owned-baseline-violations", action="store_true")
    _ = parser.add_argument("--owners", default="9-15")
    _ = parser.add_argument("--require-owner-clean", type=int)
    _ = parser.add_argument("--require-zero-unresolved", action="store_true")
    _ = parser.add_argument("--write-manifest", action="store_true")
    namespace = parser.parse_args()
    return Arguments(
        patterns=cast(Path, namespace.patterns),
        manifest=cast(Path | None, namespace.manifest),
        scanner_lock=cast(Path, namespace.scanner_lock),
        scanner_attestation=cast(Path | None, namespace.scanner_attestation),
        baseline=cast(str, namespace.baseline),
        allow_owned_baseline_violations=cast(
            bool,
            namespace.allow_owned_baseline_violations,
        ),
        owners=cast(str, namespace.owners),
        require_owner_clean=cast(int | None, namespace.require_owner_clean),
        require_zero_unresolved=cast(bool, namespace.require_zero_unresolved),
        write_manifest=cast(bool, namespace.write_manifest),
    )


def main() -> int:
    """Verify or print a frozen manifest."""
    args = parse_arguments()
    root = Path.cwd().resolve()
    try:
        patterns_path = args.patterns.resolve()
        lock_path = args.scanner_lock.resolve()
        attestation_path = (
            args.scanner_attestation.resolve()
            if args.scanner_attestation is not None
            else root / ".omo/runtime/provider-scanner-resolved.json"
        )
        patterns = load_object(patterns_path)
        scanner = verify_scanner(lock_path, attestation_path)
        selected_modes = sum(
            (
                bool(args.allow_owned_baseline_violations),
                args.require_owner_clean is not None,
                bool(args.require_zero_unresolved),
            )
        )
        if selected_modes != 1:
            raise InventoryError("select exactly one provider-boundary enforcement mode")
        if (
            args.require_owner_clean is not None
            and args.require_owner_clean not in range(9, 16)
        ):
            raise InventoryError("require-owner-clean must be Todo 9 through 15")
        allowed_owners = parse_owners(args.owners)
        baseline_rows = scan_baseline(root, args.baseline, patterns, scanner)
        expected_manifest = generated_manifest(args.baseline, patterns_path, lock_path, baseline_rows)
        if args.write_manifest:
            _ = sys.stdout.buffer.write(canonical(expected_manifest))
            return 0
        if args.manifest is None:
            raise InventoryError("--manifest is required outside --write-manifest")
        manifest = load_object(args.manifest.resolve())
        if manifest != expected_manifest:
            raise InventoryError("manifest count/hash/computed-shape drift")
        entrypoints_value = manifest.get("entrypoints")
        if not isinstance(entrypoints_value, list) or not all(isinstance(row, dict) for row in entrypoints_value):
            raise InventoryError("manifest entrypoints must be objects")
        entrypoints = [row for row in entrypoints_value if isinstance(row, dict)]
        assigned_paths = {
            path
            for assigned in range(9, 16)
            for path in paths_for_owner(manifest, assigned)
        }
        if len(assigned_paths) != len(entrypoints):
            raise InventoryError("every baseline entrypoint requires one unique owner")
        baseline_by_path = {string_value(row, "path"): row for row in entrypoints}
        baseline_findings = {
            path: finding_multiset(
                path,
                run_git(root, "show", f"{args.baseline}:{path}", binary=True),
                patterns,
            )
            for path in baseline_by_path
        }
        quarantine_source = root / QUARANTINE_PATH
        if not quarantine_source.is_file() or quarantine_source.is_symlink():
            raise InventoryError("paid compatibility quarantine must be a regular file")
        validate_quarantine_source(quarantine_source.read_bytes())
        reject_reverse_quarantine_imports(root, patterns)
        current_rows = scan_worktree(root, patterns, scanner)
        unresolved: list[str] = []
        quarantine_findings = 0
        for row in current_rows:
            path = string_value(row, "path")
            if path == QUARANTINE_PATH:
                quarantine_findings += 1
                continue
            frozen = baseline_by_path.get(path)
            if not isinstance(frozen, dict):
                unresolved.append(f"unowned new provider shape: {path}")
                continue
            added_findings = (
                finding_multiset(path, (root / path).read_bytes(), patterns)
                - baseline_findings[path]
            )
            if added_findings:
                unresolved.append(f"changed provider finding multiset: {path}")
            new_reasons = set(string_list(row, "discovery_reasons")) - set(string_list(frozen, "discovery_reasons"))
            if new_reasons:
                unresolved.append(f"changed provider shape: {path}: {sorted(new_reasons)}")
            owner_value = frozen.get("owner_todo")
            if not isinstance(owner_value, int) or isinstance(owner_value, bool):
                raise InventoryError(f"integer owner required: {path}")
            assigned = owner_value
            if args.require_zero_unresolved or args.require_owner_clean == assigned:
                unresolved.append(f"owner {assigned} remains unresolved: {path}")
            if args.allow_owned_baseline_violations and assigned not in allowed_owners:
                unresolved.append(f"owner outside allowance: {path}: {assigned}")
        poison_imported: list[str] = []
        try:
            if args.require_owner_clean is not None:
                poison_imported = poison_import_paths(
                    root,
                    paths_for_owner(manifest, args.require_owner_clean),
                    scanner,
                )
            elif args.require_zero_unresolved:
                poison_imported = poison_import_paths(
                    root,
                    sorted(assigned_paths),
                    scanner,
                )
            else:
                poison_imported = poison_import_modules(
                    root,
                    (*CLEAN_DEFAULT_MODULES, "pipeline.providers.paid_compat"),
                )
        except InventoryError as error:
            unresolved.append(str(error))
        if unresolved:
            raise InventoryError("; ".join(sorted(set(unresolved))))
        baseline_value = object_value(expected_manifest, "baseline")
        lexical_value = baseline_value.get("lexical_file_count")
        if not isinstance(lexical_value, int) or isinstance(lexical_value, bool):
            raise InventoryError("integer lexical count required")
        summary: JsonObject = {
            "baseline_entrypoints": len(baseline_rows),
            "baseline_lexical_files": lexical_value,
            "result": "PASS",
            "unowned": 0,
            "worktree_entrypoints": len(current_rows),
            "clean_default_modules": [module for module in CLEAN_DEFAULT_MODULES],
            "poison_imported": len(poison_imported),
            "quarantine_findings": quarantine_findings,
        }
        _ = sys.stdout.buffer.write(canonical(summary))
    except (OSError, KeyError, ValueError, json.JSONDecodeError, InventoryError) as error:
        print(f"Provider inventory denied: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
