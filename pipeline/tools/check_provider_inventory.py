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
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
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


def verify_scanner(root: Path, lock_path: Path, attestation_path: Path) -> JsonObject:
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
    prefix = (root / ".tools/provider-scanner").resolve()
    if prefix not in acorn.parents or string_value(scanner, "acorn_file_url") != acorn.as_uri():
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
            row: JsonObject = {"discovery_reasons": reason_values, "path": path}
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
    return owners


def main() -> int:
    """Verify or print a frozen manifest."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--patterns", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--scanner-lock", type=Path, required=True)
    parser.add_argument("--scanner-attestation", type=Path)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--allow-owned-baseline-violations", action="store_true")
    parser.add_argument("--owners", default="9-15")
    parser.add_argument("--require-owner-clean", type=int)
    parser.add_argument("--require-zero-unresolved", action="store_true")
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()
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
        scanner = verify_scanner(root, lock_path, attestation_path)
        baseline_rows = scan_baseline(root, args.baseline, patterns, scanner)
        expected_manifest = generated_manifest(args.baseline, patterns_path, lock_path, baseline_rows)
        if args.write_manifest:
            sys.stdout.buffer.write(canonical(expected_manifest))
            return 0
        if args.manifest is None:
            raise InventoryError("--manifest is required outside --write-manifest")
        manifest = load_object(args.manifest.resolve())
        if manifest != expected_manifest:
            raise InventoryError("manifest count/hash/computed-shape drift")
        allowed_owners = parse_owners(args.owners)
        entrypoints_value = manifest.get("entrypoints")
        if not isinstance(entrypoints_value, list) or not all(isinstance(row, dict) for row in entrypoints_value):
            raise InventoryError("manifest entrypoints must be objects")
        entrypoints = [row for row in entrypoints_value if isinstance(row, dict)]
        baseline_by_path = {string_value(row, "path"): row for row in entrypoints}
        current_rows = scan_worktree(root, patterns, scanner)
        unresolved: list[str] = []
        for row in current_rows:
            path = string_value(row, "path")
            frozen = baseline_by_path.get(path)
            if not isinstance(frozen, dict):
                unresolved.append(f"unowned new provider shape: {path}")
                continue
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
        }
        sys.stdout.buffer.write(canonical(summary))
    except (OSError, KeyError, ValueError, json.JSONDecodeError, InventoryError) as error:
        print(f"Provider inventory denied: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
