#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
# ─── How to run ───
# 1. Use the repository-attested Python 3.12 runtime.
# 2. Run: .tools/python312/python.exe pipeline/tools/resolve_runtime_policy.py --config config.json --json-out policy.json
# ──────────────────
"""Resolve runtime policy without importing providers or reading credentials."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.runtime_policy import JsonObject, RuntimePolicyError, denial_envelope, resolve_runtime_policy  # noqa: E402


def load_config(path: Path) -> JsonObject:
    """Read one JSON object without consulting environment variables."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimePolicyError(code="invalid-config", detail="config root must be a JSON object")
    return value


def publish(path: Path, payload: JsonObject) -> None:
    """Atomically publish only the caller-named result artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    """Parse CLI policy inputs and return 0 allowed or 2 denied."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--llm-mode")
    parser.add_argument("--acknowledge-paid-api-cost", action="store_true")
    args = parser.parse_args()
    denial_code: str | None = None
    try:
        config = load_config(args.config.resolve())
        policy = resolve_runtime_policy(
            config,
            cli_mode=args.llm_mode,
            paid_acknowledged=args.acknowledge_paid_api_cost,
        )
        payload = policy.envelope()
        result = 0
    except (OSError, json.JSONDecodeError, RuntimePolicyError) as error:
        policy_error = error if isinstance(error, RuntimePolicyError) else RuntimePolicyError(
            code="invalid-config",
            detail=type(error).__name__,
        )
        payload = denial_envelope(policy_error)
        denial_code = policy_error.code
        result = 2
    publish(args.json_out.resolve(), payload)
    if denial_code is not None:
        print(f"Runtime policy denied: {denial_code}", file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
