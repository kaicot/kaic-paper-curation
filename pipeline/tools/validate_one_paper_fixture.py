#!/usr/bin/env python3
"""Todo 24 — validate the tracked one-paper fixture.

Verifies the exact seed files and their recorded sha256, runs a secret scan
over the seed, and checks the expected-artifact manifest is complete and
well-formed. Todo 25/F3 re-run this validator before copying the fixture
path and reject any absent/stale default artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)api[_-]?key[\"'\s:=]+[A-Za-z0-9_\-]{16,}"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    fixture = args.fixture.resolve()
    manifest_path = fixture / "expected-artifacts-v1.json"
    failures: list[str] = []
    if not manifest_path.is_file():
        failures.append("expected-artifacts-v1.json missing")
        _emit(failures, args.json_out)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "one-paper-fixture-v1" or manifest.get("schema_version") != 1:
        failures.append("manifest schema invalid")
    if not manifest.get("paper_slug") or not manifest.get("required_artifacts"):
        failures.append("manifest missing slug or required artifacts")

    for relative in manifest.get("seed_files", []):
        path = fixture / "seed" / relative
        if not path.is_file():
            failures.append(f"seed file missing: {relative}")
            continue
        raw = path.read_bytes()
        # Normalize line endings so LF (worktree) and CRLF (checked-out) copies
        # hash identically; recorded hashes are LF-normalized.
        normalized = raw.replace(b"\r\n", b"\n")
        actual = hashlib.sha256(normalized).hexdigest()
        recorded = manifest.get("seed_sha256", {}).get(relative)
        if recorded != actual:
            failures.append(f"seed hash drift: {relative} {actual} != {recorded}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(raw.decode("utf-8", "replace")):
                failures.append(f"secret shape in seed: {relative}")

    payload = {
        "schema": "one-paper-fixture-validation-v1",
        "schema_version": 1,
        "fixture": str(fixture),
        "seed_files": manifest.get("seed_files", []),
        "required_artifacts": manifest.get("required_artifacts", []),
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    sys.stdout.write(text + "\n")
    return 0 if not failures else 1


def _emit(failures: list[str], out: Path | None) -> None:
    payload = {"schema": "one-paper-fixture-validation-v1", "schema_version": 1, "result": "FAIL", "failures": failures}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    sys.stdout.write(text + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
