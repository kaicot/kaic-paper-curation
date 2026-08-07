#!/usr/bin/env python3
"""Todo 23 — final-review command runner.

Runs exactly one F1-F4 command/negative/cleanup with the exact required
CLI contract and emits a canonical command-evidence envelope plus raw
stdout/stderr streams (all create-new, never overwrite). A quiet exit-0
command still yields a nonempty envelope.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIREMENTS = REPO_ROOT / "pipeline" / "final-review-requirements-v1.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def load_requirements() -> dict:
    return json.loads(REQUIREMENTS.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verifier", required=True, choices=["F1", "F2", "F3", "F4"])
    parser.add_argument("--command-id")
    parser.add_argument("--negative-id")
    parser.add_argument("--cleanup-id")
    parser.add_argument("--envelope-out", type=Path, required=True)
    parser.add_argument("--stdout-out", type=Path, required=True)
    parser.add_argument("--stderr-out", type=Path, required=True)
    parser.add_argument("executable", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    selectors = [s for s in (args.command_id, args.negative_id, args.cleanup_id) if s]
    if len(selectors) != 1:
        sys.stderr.write("exactly one ID selector required\n")
        return 2
    kind, command_id = ("command", args.command_id) if args.command_id else (
        ("negative", args.negative_id) if args.negative_id else ("cleanup", args.cleanup_id)
    )

    requirements = load_requirements()
    verifier = requirements["verifiers"].get(args.verifier)
    if verifier is None:
        sys.stderr.write(f"unknown verifier: {args.verifier}\n")
        return 2
    pool = verifier["commands"] if kind == "command" else (
        verifier["negatives"] if kind == "negative" else verifier["cleanup"]
    )
    if command_id not in pool:
        sys.stderr.write(f"unknown {kind} id {command_id!r} for {args.verifier}\n")
        return 2

    argv = args.executable
    if not argv or argv[0] != "--":
        sys.stderr.write("-- <executable-and-argv> required\n")
        return 2
    child_argv = argv[1:]

    started = now()
    result = subprocess.run(
        child_argv,
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=3600,
        check=False,
    )
    ended = now()
    stdout = result.stdout
    stderr = result.stderr

    for out, payload in ((args.stdout_out, stdout), (args.stderr_out, stderr)):
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("xb") as stream:
            stream.write(payload)

    envelope = {
        "schema_version": 1,
        "command_id": command_id,
        "argv": child_argv,
        "cwd": str(REPO_ROOT),
        "started_at_utc": started,
        "ended_at_utc": ended,
        "exit_code": result.returncode,
        "stdout_size": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_size": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }
    args.envelope_out.parent.mkdir(parents=True, exist_ok=True)
    with args.envelope_out.open("xb") as stream:
        stream.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    # command/cleanup expect exit 0; negative expects nonzero.
    expected = 0 if kind != "negative" else 1
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if (result.returncode == 0) == (expected == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
